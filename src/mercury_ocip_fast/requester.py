import attrs
import logging
import asyncio
import time
from itertools import batched
from typing import Union

from mercury_ocip_fast.pool import PoolConfig, TCPConnectionPool
from mercury_ocip_fast.exceptions import MErrorSocketTimeout, MError

_XML_DECLARATION = b'<?xml version="1.0" encoding="ISO-8859-1"?>'
_BROADSOFT_DOC_START = b'<BroadsoftDocument protocol="OCI" xmlns="C" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
_BROADSOFT_DOC_END = b"</BroadsoftDocument>"
_SESSION_ID_TEMPLATE = b'<sessionId xmlns="">%s</sessionId>'


@attrs.define(kw_only=True)
class AsyncTCPRequester:
    """A requester for BroadWorks OCI-P.

    Args:
        host: The Broadworks Server IP (e.g adp.broadworks.com).
        port: The port of the Broadworks Server, usually 2208 for non-TLS / 2209 for TLS.
        config: The timeout and general pool settings.
        tls: Whether to use a secure wrapped socket.
        session_id: The session_id to send in requests.
        logger: The logger object to retrieve logs and information.
    """

    host: str
    port: int
    config: PoolConfig = attrs.Factory(PoolConfig)
    tls: bool = True
    logger: logging.Logger
    session_id: str
    _pool: TCPConnectionPool | None = attrs.field(default=None, alias="_pool")
    _session_id_bytes: bytes | None = attrs.field(
        default=None, alias="_session_id_bytes"
    )

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
        )

        self._session_id_bytes: bytes = self.session_id.encode("ISO-8859-1")

    async def warm(self, count: int | None = None) -> int:
        """Pre-warm the connection pool for faster bulk requests.

        Args:
            count: Number of connections to create. Defaults to pool max.

        Returns:
            Number of connections created.
        """
        if self._pool is None:
            return 0
        return await self._pool.warm(count)

    async def close(self) -> None:
        """Disconnects from the server and closes the pool."""
        if self._pool:
            try:
                await self._pool.close()
                self.logger.debug("Connection pool closed")
            except Exception as e:
                self.logger.warning(f"Error closing connection pool: {e}")

    async def send_request(self, command: str) -> str:
        """Sends a request to the server.

        Args:
            command (str): The XML command string to send to the server.

        Returns:
            The response from the server as a decoded string.

        Raises:
            MErrorSendRequestFailed: If the request fails to send.
            MErrorSocketTimeout: If the socket read times out.
        """
        self.logger.debug(f"Sending command to {self.host}")
        return await self._send_bytes(self._build_oci_xml(command))

    async def send_bulk_request(
        self, commands: list[str], batch_size: int = 15
    ) -> list[str]:
        """Sends multiple requests to the server in concurrent batches.

        Batches are variable but default to 15 as per the OCIP Spec: "4.3: It is recommended to limit the number of actions to no more than 15 transactions"

        Args:
            commands (list[str]): The commands to send to the server.
            batch_size (int): The amount of commands per message.

        Returns:
            List of responses from the server, one per batch (order preserved).

        Raises:
            MError: If the pool fails to initialise.
            MErrorSocketTimeout: If the socket read times out.
        """
        chunks = [list(chunk) for chunk in batched(commands, n=batch_size)]

        self.logger.info(
            f"Sending bulk request: {len(commands)} commands in {len(chunks)} batches "
            f"(batch_size={batch_size})"
        )

        start = time.monotonic()
        tasks = [self._send_bytes(self._build_oci_xml(chunk)) for chunk in chunks]
        results = await asyncio.gather(*tasks)
        elapsed = time.monotonic() - start

        self.logger.info(
            f"Bulk request complete: {len(chunks)} batches in {elapsed:.2f}s "
            f"({len(commands) / elapsed:.1f} cmd/s)"
        )

        return results

    async def _send_bytes(self, payload: bytes) -> str:
        """Sends bytes message to Broadworks Server

        Args:
            payload (bytes): The message/commands to send to the server encoded as ISO-8859-1

        Returns:
            response (str): The response from the server

        Raises:
            MError: If the pool fails to init
            MErrorSocketTimeout: If the socket read times out.
        """
        if self._pool is None:
            raise MError("Pool failed to initialise")

        async with self._pool.acquire() as conn:
            self.logger.debug(f"Sending {len(payload)} bytes to {self.host}")

            conn.writer.writelines([payload, b"\n"])
            await conn.writer.drain()

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
                    raise MErrorSocketTimeout(str(e))

                if not chunk:
                    break

                content.extend(chunk)

                if b"</BroadsoftDocument>" in content:
                    break

            self.logger.debug(f"Received {len(content)} bytes from {self.host}")

            return content.rstrip(b"\n").decode("ISO-8859-1")

    def _build_oci_xml(self, commands: Union[str, list[str]]) -> bytes:
        """Builds an OCI XML request from the given command(s).

        Constructs an XML document with a session ID and the encoded command(s),
        wrapped in a BroadsoftDocument element with the OCI protocol.

        Args:
            commands (str | list[str]): A single command string or list of command strings.

        Returns:
            The serialized XML document as bytes, encoded with ISO-8859-1.
        """
        if isinstance(commands, list):
            commands_payload = "\n".join(commands).encode("ISO-8859-1")
        else:
            commands_payload = commands.encode("ISO-8859-1")

        return b"".join(
            [
                _XML_DECLARATION,
                _BROADSOFT_DOC_START,
                _SESSION_ID_TEMPLATE % self._session_id_bytes,
                commands_payload,
                _BROADSOFT_DOC_END,
            ]
        )
