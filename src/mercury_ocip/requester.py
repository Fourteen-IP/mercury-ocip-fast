import attr
import logging
import asyncio
from itertools import batched
from typing import Union

from mercury_ocip.pool import PoolConfig, TCPConnectionPool
from mercury_ocip.exceptions import MErrorSocketTimeout, MErrorSendRequestFailed

_XML_DECLARATION = b'<?xml version="1.0" encoding="ISO-8859-1"?>'
_BROADSOFT_DOC_START = b'<BroadsoftDocument protocol="OCI" xmlns="C" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
_BROADSOFT_DOC_END = b"</BroadsoftDocument>"
_SESSION_ID_TEMPLATE = b'<sessionId xmlns="">%s</sessionId>'


@attr.s(slots=True, kw_only=True)
class AsyncTCPRequester:
    """A requester for BroadWorks OCI-P.

    Args:
        host (str): The Broadworks Server IP (e.g adp.broadworks.com)
        port (int): The port of the Broadworks Server, usually 2208 for non tls / 2209 for tls
        config (PoolConfig): The timeout and general pool settings
        tls (bool): Whether to use a secure wrapped socket, recommened: True
        session_id (str): The session_id to send in requests
        logger (Logger): The logger object to retrieve logs and information.
    """

    host: str = attr.ib()
    port: int = attr.ib()
    config: PoolConfig = attr.ib(default=PoolConfig())
    tls: bool = attr.ib(default=True)
    logger: logging.Logger = attr.ib()
    session_id: str = attr.ib()
    _pool: TCPConnectionPool | None = attr.ib(default=None)
    _session_id_bytes: bytes | None = attr.ib(default=None)

    def __attrs_post_init__(self):
        self._pool = TCPConnectionPool(
            host=self.host,
            port=self.port,
            config=self.config,
            tls=self.tls,
            logger=self.logger,
        )

        self._session_id_bytes: bytes = self.session_id.encode("ISO-8859-1")

    async def close(self) -> None:
        """Disconnects from the server and closes the pool."""
        if self._pool:
            try:
                self._pool.close()
                self.logger.debug("Connection pool closed")
            except Exception as e:
                self.logger.warning(f"Error closing connection pool: {e}")

    async def send_request(self, command: str) -> str:
        """Sends a request to the server.

        Args:
            command: The XML command string to send to the server.

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
        """Sends multiple requests to the server in batches.

        Batches are variable but default to 15 as per the OCIP Spec: "4.3: It is recommended to limit the number of actions to no more than 15 transactions"

        Args:
            commands (list[str]): The commands to send to the server.
            batch_size (int): The amount of commands per message

        Returns:
            List of responses from the server, one per batch.

        Raises:
            MErrorSendRequestFailed: If any batch fails to send.
            MErrorSocketTimeout: If the socket read times out.
        """
        chunks = [list(chunk) for chunk in batched(commands, n=batch_size)]
        results: list[str] = []

        self.logger.debug(f"Sending {len(commands)} commands in {len(chunks)} batches")

        for i, chunk in enumerate(chunks, 1):
            self.logger.debug(
                f"Sending batch {i}/{len(chunks)} ({len(chunk)} commands)"
            )
            response = await self._send_bytes(self._build_oci_xml(chunk))
            results.append(response)

        return results

    async def _send_bytes(self, payload: bytes) -> str:
        if self._pool is None:
            raise MErrorSendRequestFailed("Pool failed to initialise")

        async with self._pool.acquire() as conn:
            self.logger.debug(f"Sending {len(payload)} bytes to {self.host}")

            conn.writer.write(payload + b"\n")
            await conn.writer.drain()

            content = b""

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

                content += chunk

                if b"</BroadsoftDocument>" in content:
                    break

            self.logger.debug(f"Received {len(content)} bytes from {self.host}")
            return content.rstrip(b"\n").decode("ISO-8859-1")

    def _build_oci_xml(self, commands: Union[str, list[str]]) -> bytes:
        """Builds an OCI XML request from the given command(s).

        Constructs an XML document with a session ID and the encoded command(s),
        wrapped in a BroadsoftDocument element with the OCI protocol.

        Args:
            commands: A single command string or list of command strings.

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
