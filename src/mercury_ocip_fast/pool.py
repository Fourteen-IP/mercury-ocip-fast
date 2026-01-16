from __future__ import annotations

import asyncio
import attr
import logging
import ssl
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Awaitable

from mercury_ocip_fast.exceptions import (
    MErrorSocketInitialisation,
    MErrorSocketTimeout,
)


@dataclass(slots=True)
class PoolConfig:
    """Configuration for the connection pool.

    Attributes:
        max_connections: Maximum connections in pool.
        max_concurrent_requests: Maximum simultaneous in-flight requests.
        connect_timeout: Seconds to establish TCP connection.
        read_timeout: Seconds to wait for response.
        acquire_timeout: Seconds to wait for available connection.
        max_connection_age: Recycle connections after this many seconds.
        idle_timeout: Close connections idle longer than this many seconds.
        read_chunk_size: Bytes to read per chunk during response.
    """

    max_connections: int = 50
    max_concurrent_requests: int = 100
    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    acquire_timeout: float = 5.0
    max_connection_age: float = 300.0
    idle_timeout: float = 60.0
    read_chunk_size: int = 8192


@dataclass(slots=True)
class PooledConnection:
    """A pooled TCP connection wrapper.

    Attributes:
        reader: asyncio StreamReader for receiving data.
        writer: asyncio StreamWriter for sending data.
        created_at: Timestamp when connection was established.
        last_used: Timestamp of last successful operation.
        in_use: Whether this connection is currently checked out.
    """

    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    created_at: float = field(default_factory=time.monotonic)
    last_used: float = field(default_factory=time.monotonic)
    in_use: bool = False

    def is_stale(self, max_age_seconds: float) -> bool:
        """Check if connection has exceeded its maximum lifetime.

        Args:
            max_age_seconds: Maximum age before connection is considered stale

        Returns:
            True if connection should be recycled
        """

        return (time.monotonic() - self.created_at) > max_age_seconds

    def idle_time(self) -> float:
        """How long since this connection was last used."""
        return time.monotonic() - self.last_used

    def touch(self) -> None:
        """Update last_used timestamp after successful operation."""
        self.last_used = time.monotonic()

    def is_healthy(self) -> bool:
        """Check if the connection is likely still alive.

        Returns:
            False if connection is definitely dead, True if it might be alive.
        """
        if self.writer.is_closing():
            return False

        try:
            if self.reader.at_eof():
                return False
        except Exception:
            return False

        return True

    async def close(self) -> None:
        """Gracefully close the underlying TCP connection."""
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            # Connection might already be dead
            pass


@attr.s(slots=True, kw_only=True)
class TCPConnectionPool:
    """Async TCP connection pool for BroadWorks OCI-P connections.

    Manages a pool of reusable TCP connections with configurable limits,
    timeouts, and connection lifecycle management.
    """

    host: str = attr.ib()
    port: int = attr.ib()
    config: PoolConfig = attr.ib(factory=PoolConfig)
    tls: bool = attr.ib(default=True)
    logger: logging.Logger = attr.ib()
    auth_callback: Callable[[PooledConnection], Awaitable[None]] | None = attr.ib(
        default=None
    )
    _pool: asyncio.LifoQueue[PooledConnection] = attr.ib(factory=asyncio.LifoQueue)
    _semaphore: asyncio.Semaphore = attr.ib(default=None)
    _lock: asyncio.Lock = attr.ib(factory=asyncio.Lock)
    _all_connections: list[PooledConnection] = attr.ib(factory=list)
    _waiters: list[asyncio.Future[PooledConnection]] = attr.ib(factory=list)
    _closed: bool = attr.ib(default=False)

    def __attrs_post_init__(self):
        self._semaphore = asyncio.Semaphore(self.config.max_concurrent_requests)
        self.logger.info(
            f"Pool initialized for {self.host}:{self.port} "
            f"(max_conn={self.config.max_connections}, max_concurrent={self.config.max_concurrent_requests})"
        )

    async def _create_conn(self) -> PooledConnection:
        """Create a new TCP connection to the BroadWorks server.

        Returns:
            A new PooledConnection wrapping the TCP stream.

        Raises:
            MErrorSocketInitialisation: Connection timeout or OS error.
        """
        ssl_context = ssl.create_default_context() if self.tls else None

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port, ssl=ssl_context),
                timeout=self.config.connect_timeout,
            )
        except asyncio.TimeoutError as e:
            raise MErrorSocketInitialisation(
                f"Connection timeout after {self.config.connect_timeout}s"
            ) from e
        except OSError as e:
            raise MErrorSocketInitialisation(f"Connection failed: {e}") from e

        self.logger.debug(f"Created connection to {self.host}:{self.port}")
        return PooledConnection(reader=reader, writer=writer)

    async def _get_or_create_conn(self) -> PooledConnection:
        """Get an available connection from the pool, or create a new one.

        Returns:
            A PooledConnection ready for use.

        Raises:
            MErrorSocketTimeout: Timed out waiting for available connection.
        """
        waiter: asyncio.Future[PooledConnection] | None = None
        conns_to_close: list[PooledConnection] = []

        async with self._lock:
            while True:
                try:
                    conn = self._pool.get_nowait()

                    if conn.is_stale(self.config.max_connection_age):
                        self.logger.debug("Discarding stale connection")
                        self._all_connections.remove(conn)
                        conns_to_close.append(conn)
                        continue

                    if conn.idle_time() > self.config.idle_timeout:
                        self.logger.debug("Discarding idle connection")
                        self._all_connections.remove(conn)
                        conns_to_close.append(conn)
                        continue

                    if not conn.is_healthy():
                        self.logger.debug("Discarding unhealthy connection")
                        self._all_connections.remove(conn)
                        conns_to_close.append(conn)
                        continue

                    conn.in_use = True
                    self.logger.debug(
                        f"Reusing pooled connection (pool size: {self._pool.qsize()})"
                    )

                    if conns_to_close:
                        asyncio.create_task(self._close_connections(conns_to_close))
                    return conn

                except asyncio.QueueEmpty:
                    break

            if len(self._all_connections) < self.config.max_connections:
                self.logger.debug(
                    f"Creating new connection ({len(self._all_connections) + 1}/{self.config.max_connections})"
                )
                conn = await self._create_conn()

                if self.auth_callback:
                    await self.auth_callback(conn)

                conn.in_use = True
                self._all_connections.append(conn)

                if conns_to_close:
                    asyncio.create_task(self._close_connections(conns_to_close))
                return conn

            # Pool exhausted - register as waiter before releasing lock
            self.logger.debug("Pool exhausted, waiting for available connection")
            waiter = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)

        if conns_to_close:
            asyncio.create_task(self._close_connections(conns_to_close))

        # Wait outside the lock so connections can be returned
        try:
            conn = await asyncio.wait_for(waiter, timeout=self.config.acquire_timeout)
            conn.in_use = True
            return conn
        except asyncio.TimeoutError:
            # Remove from waiters if still present
            async with self._lock:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)
            raise MErrorSocketTimeout(
                f"Timeout waiting for connection after {self.config.acquire_timeout}s"
            )

    async def _close_remove_conn(self, conn: PooledConnection) -> None:
        """Close and remove a connection from the Pool."""
        await conn.close()
        self._all_connections.remove(conn)

    async def _close_connections(self, conns: list[PooledConnection]) -> None:
        """Close multiple connections concurrently (fire-and-forget cleanup)."""
        for conn in conns:
            try:
                await conn.close()
            except Exception:
                pass  # Connection might already be dead

    async def _return_connection(
        self, conn: PooledConnection, healthy: bool = True
    ) -> None:
        """Return a connection to the pool after use.

        Args:
            conn: The connection to return.
            healthy: False if an error occurred (connection may be broken).
        """
        conn.in_use = False

        if not healthy:
            self.logger.warning("Closing unhealthy connection")
            async with self._lock:
                await self._close_remove_conn(conn)
            return

        if self._closed:
            self.logger.debug("Pool closed, discarding connection")
            async with self._lock:
                await self._close_remove_conn(conn)
            return

        if conn.is_stale(self.config.max_connection_age):
            self.logger.debug("Discarding stale connection on return")
            async with self._lock:
                await self._close_remove_conn(conn)
            return

        conn.touch()

        async with self._lock:
            while self._waiters:
                waiter = self._waiters.pop(0)
                if not waiter.done():
                    self.logger.debug(
                        f"Handing connection to waiter ({len(self._waiters)} still waiting)"
                    )
                    waiter.set_result(conn)
                    return

            # No waiters, return to pool
            try:
                self._pool.put_nowait(conn)
                self.logger.debug(
                    f"Returned connection to pool (pool size: {self._pool.qsize()})"
                )
            except asyncio.QueueFull:
                self.logger.warning("Pool queue full, closing connection")
                await self._close_remove_conn(conn)

    @asynccontextmanager
    async def acquire(self, existing_conn=None) -> AsyncIterator[PooledConnection]:
        """Acquire a connection from the pool.

        Usage:
            async with pool.acquire() as conn:
                conn.writer.write(data)
                await conn.writer.drain()

        Yields:
            A PooledConnection for sending/receiving data.

        Raises:
            RuntimeError: Pool has been closed.
            MErrorSocketTimeout: Timed out waiting for connection.
        """
        if self._closed:
            raise RuntimeError("Pool is closed.")

        if existing_conn:
            yield existing_conn
            return

        async with self._semaphore:
            conn: PooledConnection = await self._get_or_create_conn()
            healthy = True

            try:
                yield conn
            except Exception:
                healthy = False
                raise
            finally:
                await self._return_connection(conn, healthy)

    async def warm(self, count: int | None = None) -> int:
        """Pre-create connections to avoid cold-start latency.

        Args:
            count: Number of connections to create. Defaults to max_connections.

        Returns:
            Number of connections actually created.
        """
        if count is None:
            count = self.config.max_connections

        async with self._lock:
            existing = len(self._all_connections)
            to_create = min(count, self.config.max_connections) - existing

            if to_create <= 0:
                return 0

        self.logger.info(f"Warming pool with {to_create} connections...")

        tasks = [self._create_conn() for _ in range(to_create)]
        connections = await asyncio.gather(*tasks, return_exceptions=True)

        created = 0
        failed = 0
        async with self._lock:
            for conn in connections:
                if isinstance(conn, PooledConnection):
                    try:
                        if self.auth_callback:
                            await self.auth_callback(conn)
                        self._all_connections.append(conn)
                        self._pool.put_nowait(conn)
                        created += 1
                    except Exception as e:
                        await conn.close()
                        failed += 1
                        self.logger.warning(
                            f"Failed to authenticate connection during warm: {e}"
                        )
                else:
                    failed += 1
                    self.logger.warning(
                        f"Failed to create connection during warm: {conn}"
                    )

        self.logger.info(f"Warmed pool with {created} connections ({failed} failed)")
        return created

    async def close(self, wait_timeout: float = 10.0) -> None:
        """Close all connections and shutdown the pool.

        Args:
            wait_timeout: Maximum seconds to wait for in-flight operations to complete.
        """
        self._closed = True

        start = time.monotonic()
        in_use_count = sum(1 for conn in self._all_connections if conn.in_use)

        if in_use_count > 0:
            self.logger.info(
                f"Waiting for {in_use_count} in-use connections to be returned..."
            )

        while any(conn.in_use for conn in self._all_connections):
            if time.monotonic() - start > wait_timeout:
                remaining = sum(1 for conn in self._all_connections if conn.in_use)
                self.logger.warning(
                    f"Timeout waiting for connections to be returned ({remaining} still in use)"
                )
                break
            await asyncio.sleep(0.1)

        async with self._lock:
            for waiter in self._waiters:
                if not waiter.done():
                    waiter.cancel()
            self._waiters.clear()

            close_tasks = [conn.close() for conn in self._all_connections]
            if close_tasks:
                await asyncio.gather(*close_tasks, return_exceptions=True)
            self._all_connections.clear()

        # Drain the pool queue
        while not self._pool.empty():
            try:
                self._pool.get_nowait()
            except asyncio.QueueEmpty:
                break

        self.logger.info("Connection pool closed")

    @property
    def stats(self) -> dict[str, int]:
        """Get current pool statistics for monitoring."""
        available = self._pool.qsize()
        total = len(self._all_connections)

        return {
            "total_connections": total,
            "available": available,
            "in_use": total - available,
            "waiting": len(self._waiters),
            "max_connections": self.config.max_connections,
            "max_concurrent": self.config.max_concurrent_requests,
        }

    def __repr__(self) -> str:
        stats: dict[str, int] = self.stats
        return (
            f"ConnectionPool({self.host}:{self.port}, "
            f"connections={stats['in_use']}/{stats['total_connections']}/{self.config.max_connections})"
        )
