import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any

import attrs

from mercury_ocip_fast.exceptions import (
    MErrorPoolClosed,
    MErrorPoolExhausted,
)
from mercury_ocip_fast.session.session import SessionAtom

logger = logging.getLogger(__name__)


@attrs.define(slots=True, frozen=True)
class SessionPoolSettings:
    """Config for the Session Pool."""

    max_size: int = 5
    acquire_timeout: float = 10.0
    wait_timeout: float = 10.0


@attrs.define(slots=True)
class SessionPool[A: SessionAtom[Any]]:
    """An async pool of reusable session atoms.

    Borrow a session with `acquire` or the `session`
    context manager, use it, then return it with `release`.

    The pool recycles healthy sessions from its idle queue, creates
    new ones up to ``pool_settings.max_size``, and discards stale or
    broken ones.

    Attributes:
        pool_settings: Pool configuration (max size, timeouts).
    """

    default_factory: Callable[[], Awaitable[A]]
    pool_settings: SessionPoolSettings = attrs.field(default=SessionPoolSettings())
    _idle: asyncio.LifoQueue[A] = attrs.field(factory=asyncio.LifoQueue)
    _condition: asyncio.Condition = attrs.field(factory=asyncio.Condition)
    _semaphore: asyncio.Semaphore = attrs.field(init=False)
    _all: list[A] = attrs.field(factory=list)
    _closed: bool = attrs.field(default=False)

    def __attrs_post_init__(self) -> None:
        """Initialise the semaphore now that ``max_size`` is known."""
        self._semaphore = asyncio.Semaphore(self.pool_settings.max_size)
        logger.debug("Pool initialized (max_size=%d)", self.pool_settings.max_size)

    @property
    def _in_use(self) -> int:
        """Count of sessions currently checked out (not in the idle queue)."""
        return len(self._all) - self._idle.qsize()

    async def _make(self) -> A:
        """Create and authenticate a new session.

        Calls ``_create_fn`` to open a transport, then optionally runs
        ``_auth_fn`` to authenticate it. If authentication fails, the
        transport is closed.

        Returns:
            A new, registered session atom.

        Raises:
            Exception: Any error from ``_create_fn`` or ``_auth_fn``.
        """
        atom = await self.default_factory()

        self._all.append(atom)
        logger.debug(
            "Created new session %s (total: %d)",
            getattr(atom, "session_id", "?"),
            len(self._all),
        )
        return atom

    async def _discard(self, atom: A) -> None:
        """Close *atom* and remove it from internal bookkeeping.

        Both ``atom.close()`` and ``self._all.remove(atom)`` are wrapped
        in ``suppress`` so cleanup errors never mask the original
        failure that triggered the discard.
        """
        with suppress(Exception):
            await atom.close()
        with suppress(ValueError):
            self._all.remove(atom)

    async def _wait_for_idle(self) -> bool:
        """Block until every in-use session has been returned.

        Waits for ``_in_use`` to reach zero (all sessions back in the
        idle queue) or ``pool_settings.wait_timeout`` seconds to
        elapse, whichever comes first.

        Returns:
            ``True`` if all sessions were returned, ``False`` on timeout.
        """
        try:
            async with self._condition:
                await asyncio.wait_for(
                    self._condition.wait_for(lambda: self._in_use == 0),
                    timeout=self.pool_settings.wait_timeout,
                )
            return True
        except TimeoutError:
            return False

    @asynccontextmanager
    async def session(self) -> AsyncIterator[A]:
        """Borrow a session for the duration of an ``async with`` block.

        If the block raises, the session is marked unhealthy so it is
        discarded rather than returned to the idle queue. This prevents
        a partially-used session from being handed to the next caller.

        Yields:
            A session atom checked out from the pool.
        """
        atom = await self.acquire()
        healthy = True
        try:
            yield atom
        except BaseException:
            healthy = False
            raise
        finally:
            await self.release(atom, healthy=healthy)

    async def acquire(self) -> A:
        """Check out a session from the pool.

        Prefers reusing a healthy idle session. If the idle queue is
        empty, creates a new one (up to ``max_size``). When all slots
        are in use, blocks until one is returned or
        ``acquire_timeout`` seconds pass.

        Returns:
            A session atom (idle-reused or freshly created).

        Raises:
            MErrorPoolClosed: If `close` has already been called.
            MErrorPoolExhausted: If no session frees up within
                ``acquire_timeout`` seconds.
        """
        if self._closed:
            logger.warning("Acquire attempted on closed pool")
            raise MErrorPoolClosed("Cannot acquire from a closed pool.")

        try:
            await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout=self.pool_settings.acquire_timeout,
            )
        except TimeoutError as e:
            logger.warning(
                "No session available within %ss",
                self.pool_settings.acquire_timeout,
            )
            raise MErrorPoolExhausted(
                f"No session available within {self.pool_settings.acquire_timeout}s"
            ) from e

        try:
            while not self._idle.empty():
                atom = self._idle.get_nowait()

                if atom.is_alive() and not atom.is_stale():
                    logger.debug(
                        "Reusing idle session %s (idle: %d)",
                        getattr(atom, "session_id", "?"),
                        self._idle.qsize(),
                    )
                    return atom

                logger.debug(
                    "Discarding unhealthy idle session %s",
                    getattr(atom, "session_id", "?"),
                )
                await self._discard(atom)
            return await self._make()

        except BaseException:
            self._semaphore.release()
            raise

    async def release(self, atom: A, healthy: bool = True) -> None:
        """Return a session to the pool.

        Healthy sessions go back to the idle queue for reuse.
        Unhealthy sessions (or any session returned to a closed pool)
        are closed and removed from bookkeeping.

        Args:
            atom: The session being returned.
            healthy: Pass ``False`` if the caller knows the session is
                broken (e.g. an exception occurred while using it).
        """
        try:
            if healthy and atom.is_alive() and not self._closed:
                self._idle.put_nowait(atom)
                logger.debug(
                    "Returned session %s to pool (idle: %d)",
                    getattr(atom, "session_id", "?"),
                    self._idle.qsize(),
                )
            else:
                reason = "closed" if self._closed else "unhealthy"
                logger.debug(
                    "Discarding session %s on return (%s)",
                    getattr(atom, "session_id", "?"),
                    reason,
                )
                await self._discard(atom)
        finally:
            self._semaphore.release()

        if self._closed:
            async with self._condition:
                self._condition.notify_all()

    async def close(self) -> None:
        """Shut the pool down and close every session.

        Idle sessions are closed immediately. In-use sessions are
        given up to ``wait_timeout`` seconds to be returned.
        Anything still outstanding is force-closed.
        """
        if self._closed:
            return

        self._closed = True
        logger.info("Closing session pool...")

        while not self._idle.empty():
            await self._discard(self._idle.get_nowait())

        if self._in_use > 0:
            logger.info("Waiting for %d in-use session(s)...", self._in_use)

            if not await self._wait_for_idle():
                logger.warning(
                    "Timeout: %d session(s) still in use after %ss. Force closing...",
                    self._in_use,
                    self.pool_settings.wait_timeout,
                )
                for atom in list(self._all):
                    await self._discard(atom)

        self._all.clear()
        logger.info("Session pool closed")
