from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable

import attr
import httpx
from zeep import AsyncClient, Settings
from zeep.cache import InMemoryCache
from zeep.transports import AsyncTransport

from mercury_ocip_fast.exceptions import (
    MErrorSocketInitialisation,
    MErrorSocketTimeout,
)
from mercury_ocip_fast.pool import SOAPPoolConfig


@dataclass(slots=True)
class SOAPSession:
    """One logged-in SOAP session — the SOAP equivalent of a single TCP connection.

    BroadWorks keeps your login tied to the HTTP session (its JSESSIONID cookie),
    so every session here carries its own httpx client, its own cookie jar, and
    its own OCI-P session id. That's what lets us hold several independent logins
    at once. A session only holds the connection and a bit of bookkeeping; the
    requester is what actually sends requests over it.

    Attributes:
        zeep_client: The zeep client wired up to this session's httpx client.
        http_client: The httpx client that owns this session's cookie jar.
        session_id: The OCI-P session id stamped on every request we send.
        created_at: When the session was opened (monotonic clock).
        last_used: When we last sent something on it (monotonic clock).
        in_use: True while the session is checked out of the pool.
    """

    zeep_client: AsyncClient
    http_client: httpx.AsyncClient
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: float = field(default_factory=time.monotonic)
    last_used: float = field(default_factory=time.monotonic)
    in_use: bool = False

    @property
    def jsessionid(self) -> str | None:
        """This session's BroadWorks JSESSIONID cookie, or None before login.

        Together with ``session_id`` this makes up the session's full identity:
        BroadWorks pairs the cookie with the in-body OCI-P session id at login
        and rejects requests that carry one without the other. Export them
        together (see ``Client.export_soap_session``) or not at all.
        """
        return self.http_client.cookies.get("JSESSIONID")

    def adopt_identity(self, jsessionid: str, session_id: str) -> None:
        """Take on the identity of an existing logged-in BroadWorks session.

        Points this session's cookie jar and OCI-P session id at a login that
        happened elsewhere, so requests sent on it resume that session instead
        of needing their own login.

        Args:
            jsessionid: The JSESSIONID cookie value from the original login.
            session_id: The OCI-P session id paired with that cookie.
        """
        self.http_client.cookies.set("JSESSIONID", jsessionid)
        self.session_id = session_id

    def is_stale(self, max_age_seconds: float) -> bool:
        """Has this session been around longer than we want to keep it?"""
        return (time.monotonic() - self.created_at) > max_age_seconds

    def idle_time(self) -> float:
        """Seconds since the session was last used."""
        return time.monotonic() - self.last_used

    def touch(self) -> None:
        """Mark the session as just used."""
        self.last_used = time.monotonic()

    def is_healthy(self) -> bool:
        """Is the session still usable? (False once its httpx client is closed.)"""
        return not self.http_client.is_closed

    async def close(self) -> None:
        """Close the httpx client, taking its cookie jar and sockets with it."""
        try:
            await self.http_client.aclose()
        except Exception:
            # Already gone — nothing to do.
            pass


@attr.s(slots=True, kw_only=True)
class SOAPSessionPool:
    """A pool of logged-in SOAP sessions, run just like the TCP connection pool.

    It keeps a handful of authenticated sessions around and lends them out one
    request at a time: reuse the most recently returned one, fall back to making
    a new one until ``pool_size`` is reached, and queue up callers once the pool
    is full. Sessions that have gone stale, idle, or dead are quietly thrown away
    and replaced. Because each session handles a single request at a time,
    ``pool_size`` doubles as the concurrency limit.

    See :class:`~mercury_ocip_fast.pool.TCPConnectionPool` for the TCP twin —
    the checkout/return machinery is deliberately the same.
    """

    host: str = attr.ib()
    config: SOAPPoolConfig = attr.ib(factory=SOAPPoolConfig)
    logger: logging.Logger = attr.ib()
    auth_callback: Callable[[SOAPSession], Awaitable[None]] | None = attr.ib(
        default=None
    )
    _pool: asyncio.LifoQueue[SOAPSession] = attr.ib(factory=asyncio.LifoQueue)
    _semaphore: asyncio.Semaphore = attr.ib(default=None)
    _lock: asyncio.Lock = attr.ib(factory=asyncio.Lock)
    _all_sessions: list[SOAPSession] = attr.ib(factory=list)
    _waiters: list[asyncio.Future[SOAPSession]] = attr.ib(factory=list)
    _closed: bool = attr.ib(default=False)
    # All sessions on this host share one WSDL cache.
    _wsdl_cache: InMemoryCache = attr.ib(factory=InMemoryCache)

    def __attrs_post_init__(self):
        # One in-flight request per session, so the semaphore matches pool_size.
        self._semaphore = asyncio.Semaphore(self.config.pool_size)
        self.logger.info(
            f"SOAP session pool initialized for {self.host} "
            f"(pool_size={self.config.pool_size})"
        )

    @property
    def session_ids(self) -> list[str]:
        """The session ids of every session currently alive in the pool."""
        return [s.session_id for s in self._all_sessions]

    async def _create_session(self) -> SOAPSession:
        """Spin up a fresh session: a new httpx client and its zeep client.

        Note this doesn't log in — that's the caller's job (via auth_callback).
        Fetching the WSDL is synchronous in zeep, so we run it off-thread.

        Raises:
            MErrorSocketInitialisation: if the WSDL fetch or client setup fails.
        """
        limits = httpx.Limits(
            max_connections=self.config.max_connections,
            max_keepalive_connections=self.config.max_keepalive_connections,
            keepalive_expiry=self.config.keepalive_expiry,
        )
        timeout = httpx.Timeout(
            connect=self.config.connect_timeout,
            read=self.config.read_timeout,
            write=self.config.write_timeout,
            pool=self.config.pool_timeout,
        )
        http_client = httpx.AsyncClient(
            limits=limits, timeout=timeout, verify=self.config.verify_ssl
        )
        transport = AsyncTransport(client=http_client, cache=self._wsdl_cache)
        settings = Settings(strict=False, xml_huge_tree=True)  # type: ignore

        try:
            # zeep fetches the WSDL synchronously in __init__ — offload to a thread.
            zeep_client = await asyncio.to_thread(
                lambda: AsyncClient(
                    wsdl=f"{self.host}?wsdl", transport=transport, settings=settings
                )
            )
        except Exception as e:
            await http_client.aclose()
            raise MErrorSocketInitialisation(
                f"Failed to initialise SOAP session: {e}"
            ) from e

        self.logger.debug(f"Created SOAP session for {self.host}")
        return SOAPSession(zeep_client=zeep_client, http_client=http_client)

    async def create_detached_session(
        self, jsessionid: str, session_id: str
    ) -> SOAPSession:
        """Build a session that resumes an existing BroadWorks login.

        The session is *not* tracked by the pool and never logged in here — it
        adopts the given (JSESSIONID, session id) pair instead. The caller owns
        it and must ``close()`` it when done. Send on it by passing it straight
        to the requester: ``requester.send_request(xml, session=session)``.

        Args:
            jsessionid: The JSESSIONID cookie value from the original login.
            session_id: The OCI-P session id paired with that cookie.

        Raises:
            MErrorSocketInitialisation: if the WSDL fetch or client setup fails.
        """
        session = await self._create_session()
        session.adopt_identity(jsessionid, session_id)
        return session

    async def _get_or_create_session(self) -> SOAPSession:
        """Hand back a ready session: reuse one, make a new one, or wait for one.

        We pull from the pool first (dropping anything stale, idle, or dead along
        the way), make and log in a new session if there's still room, and only
        as a last resort queue up behind whoever's using the pool right now.

        Raises:
            MErrorSocketTimeout: if nothing frees up within ``acquire_timeout``.
        """
        sessions_to_close: list[SOAPSession] = []

        async with self._lock:
            while True:
                try:
                    session = self._pool.get_nowait()

                    if session.is_stale(self.config.max_session_age):
                        self.logger.debug("Discarding stale SOAP session")
                        self._all_sessions.remove(session)
                        sessions_to_close.append(session)
                        continue

                    if session.idle_time() > self.config.idle_timeout:
                        self.logger.debug("Discarding idle SOAP session")
                        self._all_sessions.remove(session)
                        sessions_to_close.append(session)
                        continue

                    if not session.is_healthy():
                        self.logger.debug("Discarding unhealthy SOAP session")
                        self._all_sessions.remove(session)
                        sessions_to_close.append(session)
                        continue

                    session.in_use = True
                    self.logger.debug(
                        f"Reusing pooled SOAP session (pool size: {self._pool.qsize()})"
                    )

                    if sessions_to_close:
                        asyncio.create_task(self._close_sessions(sessions_to_close))
                    return session

                except asyncio.QueueEmpty:
                    break

            if len(self._all_sessions) < self.config.pool_size:
                self.logger.debug(
                    f"Creating new SOAP session "
                    f"({len(self._all_sessions) + 1}/{self.config.pool_size})"
                )
                session = await self._create_session()

                if self.auth_callback:
                    try:
                        await self.auth_callback(session)
                    except Exception:
                        await session.close()
                        raise

                session.in_use = True
                self._all_sessions.append(session)

                if sessions_to_close:
                    asyncio.create_task(self._close_sessions(sessions_to_close))
                return session

            # Pool exhausted - register as waiter before releasing lock
            self.logger.debug("SOAP pool exhausted, waiting for available session")
            waiter = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)

        if sessions_to_close:
            asyncio.create_task(self._close_sessions(sessions_to_close))

        # Wait outside the lock so sessions can be returned
        try:
            session = await asyncio.wait_for(
                waiter, timeout=self.config.acquire_timeout
            )
            session.in_use = True
            return session
        except asyncio.TimeoutError:
            async with self._lock:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)
            raise MErrorSocketTimeout(
                f"Timeout waiting for SOAP session after {self.config.acquire_timeout}s"
            )

    async def _close_remove_session(self, session: SOAPSession) -> None:
        """Close a session and drop it from the pool's bookkeeping."""
        await session.close()
        self._all_sessions.remove(session)

    async def _close_sessions(self, sessions: list[SOAPSession]) -> None:
        """Close a batch of discarded sessions; used for fire-and-forget cleanup."""
        for session in sessions:
            try:
                await session.close()
            except Exception:
                pass  # Already gone.

    async def _return_session(self, session: SOAPSession, healthy: bool = True) -> None:
        """Take a session back once a request is done.

        If it's broken, the pool is shutting down, or it's simply too old, we
        close it. Otherwise it goes to whoever's been waiting, or back into the
        pool for the next caller.

        Args:
            session: The session being returned.
            healthy: Pass False if the request blew up and the session may be bad.
        """
        session.in_use = False

        if not healthy:
            self.logger.warning("Closing unhealthy SOAP session")
            async with self._lock:
                await self._close_remove_session(session)
            return

        if self._closed:
            self.logger.debug("Pool closed, discarding SOAP session")
            async with self._lock:
                await self._close_remove_session(session)
            return

        if session.is_stale(self.config.max_session_age):
            self.logger.debug("Discarding stale SOAP session on return")
            async with self._lock:
                await self._close_remove_session(session)
            return

        session.touch()

        async with self._lock:
            while self._waiters:
                waiter = self._waiters.pop(0)
                if not waiter.done():
                    self.logger.debug(
                        f"Handing SOAP session to waiter ({len(self._waiters)} still waiting)"
                    )
                    waiter.set_result(session)
                    return

            # No waiters, return to pool
            try:
                self._pool.put_nowait(session)
                self.logger.debug(
                    f"Returned SOAP session to pool (pool size: {self._pool.qsize()})"
                )
            except asyncio.QueueFull:
                self.logger.warning("SOAP pool queue full, closing session")
                await self._close_remove_session(session)

    @asynccontextmanager
    async def acquire(
        self, existing_session: SOAPSession | None = None
    ) -> AsyncIterator[SOAPSession]:
        """Borrow a session for the length of one request.

        Use it as a context manager so the session always finds its way back to
        the pool, even if the request raises::

            async with pool.acquire() as session:
                await session.zeep_client.service.processOCIMessage(...)

        Args:
            existing_session: Skip the pool and use this exact session instead.
                Handy during login, when we already have the session in hand.

        Raises:
            RuntimeError: if the pool has already been closed.
            MErrorSocketTimeout: if no session frees up in time.
        """
        if self._closed:
            raise RuntimeError("Pool is closed.")

        if existing_session:
            yield existing_session
            return

        async with self._semaphore:
            session: SOAPSession = await self._get_or_create_session()
            healthy = True

            try:
                yield session
            except Exception:
                healthy = False
                raise
            finally:
                await self._return_session(session, healthy)

    async def warm(self, count: int | None = None) -> int:
        """Open and log in some sessions up front so the first requests are fast.

        Without this the pool fills in lazily, one session at a time under the
        lock, and each one pays for a WSDL fetch plus a login — slow if a burst
        of requests all arrive at once.

        Args:
            count: How many sessions to open. Defaults to the full pool_size.

        Returns:
            How many sessions actually came up (failures are logged, not raised).
        """
        if count is None:
            count = self.config.pool_size

        async with self._lock:
            existing = len(self._all_sessions)
            to_create = min(count, self.config.pool_size) - existing

            if to_create <= 0:
                return 0

        self.logger.info(f"Warming SOAP pool with {to_create} sessions...")

        tasks = [self._create_session() for _ in range(to_create)]
        sessions = await asyncio.gather(*tasks, return_exceptions=True)

        created = 0
        failed = 0
        async with self._lock:
            for session in sessions:
                if isinstance(session, SOAPSession):
                    try:
                        if self.auth_callback:
                            await self.auth_callback(session)
                        self._all_sessions.append(session)
                        self._pool.put_nowait(session)
                        created += 1
                    except Exception as e:
                        await session.close()
                        failed += 1
                        self.logger.warning(
                            f"Failed to authenticate SOAP session during warm: {e}"
                        )
                else:
                    failed += 1
                    self.logger.warning(
                        f"Failed to create SOAP session during warm: {session}"
                    )

        self.logger.info(f"Warmed SOAP pool with {created} sessions ({failed} failed)")
        return created

    async def close(self, wait_timeout: float = 10.0) -> None:
        """Shut the pool down, closing every session.

        Gives in-flight requests a chance to finish first, then stops waiting
        once ``wait_timeout`` is up and closes whatever's left.

        Args:
            wait_timeout: How long to wait for busy sessions before giving up.
        """
        self._closed = True

        start = time.monotonic()
        in_use_count = sum(1 for session in self._all_sessions if session.in_use)

        if in_use_count > 0:
            self.logger.info(
                f"Waiting for {in_use_count} in-use SOAP sessions to be returned..."
            )

        while any(session.in_use for session in self._all_sessions):
            if time.monotonic() - start > wait_timeout:
                remaining = sum(1 for s in self._all_sessions if s.in_use)
                self.logger.warning(
                    f"Timeout waiting for SOAP sessions to be returned ({remaining} still in use)"
                )
                break
            await asyncio.sleep(0.1)

        async with self._lock:
            for waiter in self._waiters:
                if not waiter.done():
                    waiter.cancel()
            self._waiters.clear()

            close_tasks = [session.close() for session in self._all_sessions]
            if close_tasks:
                await asyncio.gather(*close_tasks, return_exceptions=True)
            self._all_sessions.clear()

        # Drain the pool queue
        while not self._pool.empty():
            try:
                self._pool.get_nowait()
            except asyncio.QueueEmpty:
                break

        self.logger.info("SOAP session pool closed")

    @property
    def stats(self) -> dict[str, int]:
        """A snapshot of how busy the pool is, handy for monitoring."""
        available = self._pool.qsize()
        total = len(self._all_sessions)

        return {
            "total_sessions": total,
            "available": available,
            "in_use": total - available,
            "waiting": len(self._waiters),
            "pool_size": self.config.pool_size,
        }

    def __repr__(self) -> str:
        stats: dict[str, int] = self.stats
        return (
            f"SOAPSessionPool({self.host}, "
            f"sessions={stats['in_use']}/{stats['total_sessions']}/{self.config.pool_size})"
        )
