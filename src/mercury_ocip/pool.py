from __future__ import annotations
from abc import abstractmethod, ABC

import asyncio
import attr
import logging
import ssl
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator

from mercury_ocip.exceptions import (
    MErrorSocketInitialisation,
    MErrorSocketTimeout,
)


@dataclass(slots=True)
class PoolConfig:
    """Configuration for the connection pool."""

    max_connections: int = 50  # Max connections in pool
    max_concurrent_requests: int = 100  # Max simultaneous in-flight requests

    connect_timeout: float = 10.0  # Time to establish TCP connection
    read_timeout: float = 30.0  # Time to wait for response
    acquire_timeout: float = 5.0  # Time to wait for available connection

    max_connection_age: float = 300.0  # Recycle connections after 5 minutes
    idle_timeout: float = 60.0  # Close connections idle > 1 minute

    read_chunk_size: int = 8192  # Bytes to read per chunk during response


@dataclass(slots=True)
class PooledConnection(ABC):
    """A wrapper class for a TCP Connection

    Attributes:
        reader: asyncio StreamReader for receiving data
        writer: asyncio StreamWriter for sending data
        created_at: Timestamp for when connection was established
        last_used: Timestamp of last successful operation
        in_use: Whether this connection is currently checked out
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

    async def close(self) -> None:
        """Gracefully close the underlying TCP connection."""
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            # Connection might already be dead
            pass


@attr.s(slots=True, kw_only=True)
class BaseTCPConnectionPool:
    """A Pool of TCP Socket connections"""

    host: str = attr.ib()
    port: int = attr.ib()
    config: PoolConfig = attr.ib(default=PoolConfig())
    tls: bool = attr.ib(default=True)
    logger: logging.Logger = attr.ib()
    _pool: asyncio.LifoQueue[PooledConnection] = attr.ib(factory=asyncio.LifoQueue)
    _semaphore: asyncio.Semaphore = attr.ib(default=None)  # Set in __attrs_post_init__
    _lock: asyncio.Lock = attr.ib(factory=asyncio.Lock)
    _all_connections: list[PooledConnection] = attr.ib(factory=list)
    _closed: bool = attr.ib(default=False)

    def __attrs_post_init__(self):
        # Semaphore needs config value, so initialize here
        self._semaphore = asyncio.Semaphore(self.config.max_concurrent_requests)

    @abstractmethod
    async def start(self) -> None:
        pass

    @abstractmethod
    async def create_conn(self) -> PooledConnection:
        """Create a new TCP connection to the Broadworks Server.

        Args:
            None

        Returns:
            connection (PooledConnection): The asyncio StreamdReader and StreamWriter for the connection.

        Raises:
            MErrorSocketInitialisation: Error from timeout or other OS errors
        """
        pass

    @abstractmethod
    async def _get_or_create_conn(self) -> PooledConnection:
        """Get an available connection from the pool, or create a new one.

        Args:
            None

        Raises:
            MErrorSocketTimeout: Connection creation timeout
        """
        pass

    @abstractmethod
    async def _close_remove_conn(self, conn: PooledConnection) -> None:
        """Close and remove a connection from the Pool."""
        pass

    @abstractmethod
    async def _return_conn(self, conn: PooledConnection, healthy: bool = True) -> None:
        """Return a connection to the pool after use.

        Args:
            conn: The connection to return
            healthy: False if an error occurred (connection may be broken)
        """
        pass

    @abstractmethod
    @asynccontextmanager
    async def aquire(self) -> AsyncIterator[PooledConnection]:
        try:
            yield
        finally:
            pass

    @abstractmethod
    async def close() -> None:
        pass

    @property
    @abstractmethod
    def stats(self) -> dict[str, int]:
        """Get current pool statistics for logging."""
        available: int = self._pool.qsize()
        total: int = len(self._all_connections)
        in_use: int = total - available

        return {
            "total_connections": total,
            "available": available,
            "in_use": in_use,
            "max_connections": self.config.max_connections,
            "max_concurrent": self.config.max_concurrent_requests,
        }

    @abstractmethod
    def __repr__(self) -> str:
        stats: dict[str, int] = self.stats
        return (
            f"ConnectionPool({self.host}:{self.port}, "
            f"connections={stats['in_use']}/{stats['total_connections']}/{self.config.max_connections})"
        )


class TCPConnectionPool(BaseTCPConnectionPool):
    async def _create_conn(self) -> PooledConnection:
        """Create a new TCP connection to the Broadworks Server.

        Args:
            None

        Returns:
            connection (PooledConnection): The asyncio StreamdReader and StreamWriter for the connection.

        Raises:
            MErrorSocketInitialisation: Error from timeout or other OS errors
        """

        try:
            if self.tls:
                # Create SSL context from system CA
                context: ssl.SSLContext = ssl.create_default_context()

                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(
                        host=self.host, port=self.port, ssl=context
                    ),
                    timeout=self.config.connect_timeout,
                )

                self.logger.debug(
                    msg=f"Initiated PoolConnection: {self.host}:{self.port}"
                )

                connection = PooledConnection(reader=reader, writer=writer)

                return connection
            else:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port),
                    timeout=self.config.connect_timeout,
                )

                self.logger.debug(
                    msg=f"Initiated PoolConnection: {self.host}:{self.port}"
                )

                connection = PooledConnection(reader=reader, writer=writer)

                return connection

        except asyncio.TimeoutError as e:
            raise MErrorSocketInitialisation(
                f"Connection timeout after {self.config.connect_timeout}s"
            ) from e

        except OSError as e:
            raise MErrorSocketInitialisation(f"Connection failed: {e}") from e

    async def _get_or_create_conn(self) -> PooledConnection:
        """Get an available connection from the pool, or create a new one.

        Args:
            None

        Raises:
            MErrorSocketTimeout: Connection creation timeout
        """

        async with self._lock:
            # Try to get an existing connection
            while True:
                try:
                    # get_nowait() returns immediately, raises QueueEmpty if empty
                    conn: PooledConnection = self._pool.get_nowait()

                    # Check if connection is still usable
                    if conn.is_stale(max_age_seconds=self.config.max_connection_age):
                        self.logger.debug("Discarding stale connection")
                        await self._close_remove_conn(conn)
                        continue

                    # Check if connection is dead
                    if conn.idle_time() > self.config.idle_timeout:
                        self.logger.debug("Discarding idle connection")
                        await self._close_remove_conn(conn)
                        continue

                    conn.in_use = True
                    return conn

                except asyncio.QueueEmpty:
                    break

            # Create a new connection into pool
            if len(self._all_connections) < self.config.max_connections:
                conn: PooledConnection = await self._create_conn()
                conn.in_use = True
                self._all_connections.append(conn)
                return conn

        self.logger.debug("Pool exhausted, waiting for available connection")

        try:
            conn: PooledConnection = await asyncio.wait_for(
                self._pool.get(),
                timeout=self.config.acquire_timeout,
            )
            conn.in_use = True
            return conn
        except asyncio.TimeoutError:
            raise MErrorSocketTimeout(
                f"Timeout waiting for connection after {self.config.acquire_timeout}s"
            )

    async def _close_remove_conn(self, conn: PooledConnection) -> None:
        """Close and remove a connection from the Pool."""
        await conn.close()
        self._all_connections.remove(conn)

    async def _return_connection(
        self, conn: PooledConnection, healthy: bool = True
    ) -> None:
        """Return a connection to the pool after use.

        Args:
            conn: The connection to return
            healthy: False if an error occurred (connection may be broken)
        """
        conn.in_use = False

        if not healthy or self._closed:
            await self._close_remove_conn(conn)
            return

        if conn.is_stale(max_age_seconds=self.config.max_connection_age):
            await self._close_remove_conn(conn)
            return

        conn.touch()

        try:
            self._pool.put_nowait(conn)
        except asyncio.QueueFull:
            await self._close_remove_conn(conn)

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[PooledConnection]:
        if self._closed:
            raise RuntimeError("Pool is closed.")

        async with self._semaphore:
            conn: PooledConnection = await self._get_or_create_conn()
            healthy = True

            try:
                yield conn
            except Exception:
                healthy = False
                raise
            finally:
                await self._return_connection(conn=conn, healthy=healthy)

    async def close(self) -> None:
        """Close all connections and shutdown the pool.

        Call this during FastAPI shutdown:
            @app.on_event("shutdown")
            async def shutdown():
                await pool.close()
        """
        self._closed = True

        async with self._lock:
            close_tasks = [conn.close() for conn in self._all_connections]
            if close_tasks:
                await asyncio.gather(*close_tasks, return_exceptions=True)
            self._all_connections.clear()

        while not self._pool.empty():
            try:
                self._pool.get_nowait()
            except asyncio.QueueEmpty:
                break

        self.logger.info("Connection pool closed")
