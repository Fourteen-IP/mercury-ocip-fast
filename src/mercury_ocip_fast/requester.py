import asyncio
import logging
from itertools import batched
from typing import Awaitable, Callable, Optional, Union

import attrs
import httpx

from mercury_ocip_fast.exceptions import (
    MError,
    MErrorSendRequestFailed,
    MErrorSocketTimeout,
)
from mercury_ocip_fast.pool import (
    PoolConfig,
    PooledConnection,
    SOAPPoolConfig,
    TCPConnectionPool,
)
from mercury_ocip_fast.soap_pool import SOAPSession, SOAPSessionPool


def _build_oci_xml(commands: Union[str, list[str]], session_id: str) -> str:
    """Wrap one or more OCI commands in the BroadsoftDocument envelope BroadWorks expects."""
    payload = "\n".join(commands) if isinstance(commands, list) else commands
    return (
        '<?xml version="1.0" encoding="ISO-8859-1"?>'
        '<BroadsoftDocument protocol="OCI" xmlns="C"'
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f'<sessionId xmlns="">{session_id}</sessionId>'
        f"{payload}"
        "</BroadsoftDocument>"
    )


@attrs.define(kw_only=True)
class AsyncTCPRequester:
    """Talks to BroadWorks over a raw OCI-P socket, pooling its connections.

    Args:
        host: The BroadWorks server address (e.g. adp.broadworks.com).
        port: The server port — usually 2208 for plain, 2209 for TLS.
        config: Pool sizing and timeout settings.
        tls: Whether to wrap the socket in TLS.
        session_id: The session id to send with requests.
        logger: Where to send logs.
    """

    host: str
    port: int
    config: PoolConfig = attrs.Factory(PoolConfig)
    tls: bool = True
    logger: logging.Logger
    session_id: str
    auth_callback: Callable[[PooledConnection], Awaitable[None]] | None = None
    _pool: TCPConnectionPool | None = attrs.field(default=None, alias="_pool")

    def __attrs_post_init__(self):
        self.logger.info(
            f"Initializing requester for {self.host}:{self.port} (tls={self.tls})"
        )

        self._pool = TCPConnectionPool(
            host=self.host,
            port=self.port,
            config=self.config,
            tls=self.tls,
            logger=self.logger,
            auth_callback=self.auth_callback,
        )

    async def warm(self, count: int | None = None) -> int:
        """Open connections up front, allows faster command processing instead of cold-boot.

        Args:
            count: How many connections to open. Defaults to the pool max.

        Returns:
            How many connections actually opened.
        """
        if self._pool is None:
            return 0
        return await self._pool.warm(count)

    async def close(self, wait_timeout: float = 10.0) -> None:
        """Disconnect from the server and tear the pool down.

        Args:
            wait_timeout: How long to wait for in-flight requests to finish first.
        """
        if self._pool:
            try:
                await self._pool.close(wait_timeout=wait_timeout)
                self.logger.debug("Connection pool closed")
            except Exception as e:
                self.logger.warning(f"Error closing connection pool: {e}")

    async def send_request(
        self, command: str, conn: Optional[PooledConnection] = None
    ) -> str:
        """Send one command and return the server's response.

        Args:
            command: The XML command string to send.
            conn: An existing connection to reuse (used during login).

        Returns:
            The server's response, decoded to a string.

        Raises:
            MErrorSendRequestFailed: if the request can't be sent.
            MErrorSocketTimeout: if the read times out.
        """
        self.logger.debug(f"Sending command to {self.host}")
        return await self._send_commands(command, conn=conn)

    async def send_bulk_request(
        self, commands: list[str], batch_size: int = 15
    ) -> list[str]:
        """Send a pile of commands at once, in concurrent batches.

        We default to 15 commands per batch because the OCI-P spec (4.3)
        recommends keeping each message to no more than 15 transactions.

        Args:
            commands: All the commands to send.
            batch_size: How many to pack into each message.

        Returns:
            One response per batch, in the same order they went out.

        Raises:
            MError: if the pool failed to start up.
            MErrorSocketTimeout: if a read times out.
        """
        chunks = [list(chunk) for chunk in batched(commands, n=batch_size)]

        self.logger.info(
            f"Sending bulk request: {len(commands)} commands in {len(chunks)} batches "
            f"(batch_size={batch_size})"
        )

        tasks = [self._send_commands(chunk) for chunk in chunks]
        results = await asyncio.gather(*tasks)

        return results

    async def _send_commands(
        self, commands: Union[str, list[str]], conn: Optional[PooledConnection] = None
    ) -> str:
        """Send a command (or batch) and read the reply off the socket.

        Grabs a connection, wraps the command(s) in that connection's own
        session id, writes it out, and reads until the closing document tag.

        Args:
            commands: One command string, or a list of them.
            conn: An existing connection to reuse (used during login).

        Returns:
            The server's response.

        Raises:
            MError: if the pool isn't ready or the network fails.
            MErrorSocketTimeout: if the read times out.
        """
        if self._pool is None:
            raise MError("Pool failed to initialise")

        async with self._pool.acquire(existing_conn=conn) as conn:
            payload = _build_oci_xml(commands, conn.session_id).encode("ISO-8859-1")
            self.logger.debug(
                f"Sending {len(payload)} bytes to {self.host} (session={conn.session_id})"
            )
            self.logger.debug(f">>> OUTGOING REQUEST:\n{payload.decode('ISO-8859-1')}")

            try:
                conn.writer.writelines([payload, b"\n"])
                await conn.writer.drain()
            except (ConnectionError, OSError, asyncio.TimeoutError) as e:
                self.logger.error(f"Failed to send data: {e}")
                raise MError(f"Network error while sending request: {e}") from e

            content = bytearray()

            while True:
                try:
                    chunk: bytes = await asyncio.wait_for(
                        conn.reader.read(self.config.read_chunk_size),
                        timeout=self.config.read_timeout,
                    )
                except asyncio.TimeoutError as e:
                    self.logger.error(
                        f"Socket read timed out after {self.config.read_timeout}s: {e}"
                    )
                    raise MErrorSocketTimeout(str(e)) from e
                except (ConnectionError, OSError) as e:
                    self.logger.error(f"Connection error while reading response: {e}")
                    raise MError(f"Network error while reading response: {e}") from e

                if not chunk:
                    break

                content.extend(chunk)

                if b"</BroadsoftDocument>" in content:
                    break

            self.logger.debug(f"Received {len(content)} bytes from {self.host}")

            try:
                decoded = content.rstrip(b"\n").decode("ISO-8859-1")
                self.logger.debug(f"<<< INCOMING RESPONSE:\n{decoded}")
                return decoded
            except UnicodeDecodeError as e:
                self.logger.error(f"Failed to decode response: {e}")
                raise MError(f"Invalid response encoding: {e}") from e


@attrs.define(kw_only=True)
class AsyncSOAPRequester:
    """Talks to BroadWorks over SOAP, with a pool of logged-in sessions behind it.

    Every request runs on a session borrowed from the pool (its zeep client
    already holds that session's login cookie), so up to ``config.pool_size``
    requests can be in flight at once, each on its own BroadWorks session. The
    pool logs a session in through ``auth_callback`` as soon as it creates one.
    The split matches the TCP side: the pool looks after session lifecycle, the
    requester does the actual sending.

    Args:
        host: The SOAP endpoint URL (e.g. https://bw.example.com/webservice/services/ProvisioningService).
        config: Session-pool sizing and timeouts.
        session_id: A default session id, kept only for API compatibility —
            pooled sessions each carry their own.
        logger: Where to send logs.
        auth_callback: Coroutine that logs a newly created session in.
    """

    host: str
    config: SOAPPoolConfig = attrs.Factory(SOAPPoolConfig)
    session_id: str
    logger: logging.Logger
    auth_callback: Callable[[SOAPSession], Awaitable[None]] | None = None
    _pool: SOAPSessionPool | None = attrs.field(default=None, alias="_pool")

    def __attrs_post_init__(self) -> None:
        self.logger.info(f"Initializing SOAP requester for {self.host}")
        self._pool = SOAPSessionPool(
            host=self.host,
            config=self.config,
            logger=self.logger,
            auth_callback=self.auth_callback,
        )

    @property
    def session_ids(self) -> list[str]:
        """The session ids of every logged-in session in the pool."""
        return self._pool.session_ids if self._pool else []

    async def warm(self, count: int | None = None) -> int:
        """Open and log in sessions up front. Defaults to a full pool."""
        if self._pool is None:
            return 0
        return await self._pool.warm(count)

    async def detached_session(self, jsessionid: str, session_id: str) -> SOAPSession:
        """Build a session that resumes an existing BroadWorks login.

        Not tracked by the pool and never logged in here — it adopts the given
        (JSESSIONID, session id) pair. The caller owns it and must ``close()``
        it; send on it via ``send_request(xml, session=session)``.

        Raises:
            MError: if the pool failed to initialise.
            MErrorSocketInitialisation: if the WSDL fetch or client setup fails.
        """
        if self._pool is None:
            raise MError("SOAP session pool failed to initialise")
        return await self._pool.create_detached_session(jsessionid, session_id)

    async def close(self, wait_timeout: float = 10.0) -> None:
        """Close every session and shut the pool down."""
        if self._pool:
            try:
                await self._pool.close(wait_timeout=wait_timeout)
                self.logger.debug("SOAP session pool closed")
            except Exception as e:
                self.logger.warning(f"Error closing SOAP session pool: {e}")

    async def send_request(
        self, command: str, session: Optional[SOAPSession] = None
    ) -> str:
        """Send one command on a logged-in session from the pool.

        Args:
            command: XML command string from command.to_xml().
            session: An existing session to reuse (used during login).

        Returns:
            The OCI response XML.
        """
        self.logger.debug(f"Sending SOAP command to {self.host}")
        return await self._send_soap(command, session=session)

    async def send_bulk_request(
        self, commands: list[str], batch_size: int = 15
    ) -> list[str]:
        """Send a batch of commands at once, spread across the pool's sessions.

        Args:
            commands: All the command strings to send.
            batch_size: How many to pack per SOAP message (15 max, per the spec).

        Returns:
            One response per batch, in the order they went out.
        """
        chunks = [list(chunk) for chunk in batched(commands, n=batch_size)]
        self.logger.info(
            f"Sending SOAP bulk request: {len(commands)} commands in {len(chunks)} "
            f"batches (batch_size={batch_size})"
        )
        tasks = [self._send_soap(chunk) for chunk in chunks]
        return list(await asyncio.gather(*tasks))

    async def _send_soap(
        self, commands: Union[str, list[str]], session: Optional[SOAPSession] = None
    ) -> str:
        """Borrow a session, build the envelope, and call processOCIMessage.

        Args:
            commands: One command string, or a list of them.
            session: An existing session to reuse (used during login).

        Returns:
            The OCI response XML.

        Raises:
            MError: if the pool failed to start up.
            MErrorSocketTimeout: if the HTTP request times out.
            MErrorSendRequestFailed: for any other SOAP/transport failure.
        """
        if self._pool is None:
            raise MError("SOAP session pool failed to initialise")

        async with self._pool.acquire(existing_session=session) as session:
            oci_xml = _build_oci_xml(commands, session.session_id)
            self.logger.debug(
                f">>> SOAP OCI XML (session={session.session_id}):\n{oci_xml}"
            )

            try:
                response: str = await session.zeep_client.service.processOCIMessage(
                    oci_xml
                )
            except httpx.TimeoutException as e:
                self.logger.error(f"SOAP request timed out: {e}")
                raise MErrorSocketTimeout(str(e)) from e
            except Exception as e:
                self.logger.error(f"SOAP request failed: {e}")
                raise MErrorSendRequestFailed(str(e)) from e

            self.logger.debug(f"<<< SOAP RESPONSE:\n{response}")
            return response
