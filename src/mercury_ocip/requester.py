import attr
from abc import ABC, abstractmethod
import logging
import asyncio
from typing import Union

from mercury_ocip.pool import PoolConfig, TCPConnectionPool
from mercury_ocip.libs.types import (
    RequestResult,
    DisconnectResult,
    ConnectResult,
)
from mercury_ocip.exceptions import MErrorSocketTimeout, MErrorSendRequestFailed

_XML_DECLARATION = b'<?xml version="1.0" encoding="ISO-8859-1"?>'
_BROADSOFT_DOC_START = b'<BroadsoftDocument protocol="OCI" xmlns="C" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
_BROADSOFT_DOC_END = b"</BroadsoftDocument>"
_SESSION_ID_TEMPLATE = b'<sessionId xmlns="">%s</sessionId>'


@attr.s(slots=True, kw_only=True)
class BaseRequester(ABC):
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
    _pool: TCPConnectionPool = attr.ib(default=None)
    _session_id_bytes: bytes = attr.ib(default=None)

    def __attrs_post_init__(self):
        self._pool = TCPConnectionPool(
            host=self.host,
            port=self.port,
            config=self.config,
            tls=self.tls,
            logger=self.logger,
        )

        self._session_id_bytes: bytes = self.session_id.encode(
            "ISO-8859-1"
        )  # Do this pre-emptively

    @abstractmethod
    async def send_request(self, command: str) -> RequestResult:
        """Sends a request to the server.

        Args:
            command (BroadworksCommand): The command to send to the server.
        """
        pass

    @abstractmethod
    async def send_bulk_request(self, commands: list[str]) -> list[RequestResult]:
        """Sends multiple requests to the server

        Args:
            commands (list[BroadworksCommand]): The commands to be sent to the server.
        """
        pass

    @abstractmethod
    async def connect(
        self,
    ) -> ConnectResult:
        """Connects to the server.

        Returns:
            None if successful, or a tuple of (ExceptionType, Exception) if an error occurs.
            For async implementations, returns an awaitable of the same.
        """
        pass

    @abstractmethod
    async def disconnect(self) -> DisconnectResult:
        """Disconnects from the server."""
        pass

    def build_oci_xml(self, command: Union[str, list[str]]) -> bytes:
        """Builds an OCI XML request from the given BroadworksCommand.

        Constructs an XML document with a session ID and the encoded command,
        wrapped in a BroadsoftDocument element with the OCI protocol.

        Args:
            command (BroadworksCommand): The command to be encoded into the XML.

        Returns:
            bytes: The serialized XML document as bytes, encoded with ISO-8859-1.
        """

        if isinstance(command, list):
            command_bytes_list = [command for command in command]
            commands_payload: bytes = "\n".join(command_bytes_list).encode("ISO-8859-1")

            return b"".join(
                [
                    _XML_DECLARATION,
                    _BROADSOFT_DOC_START,
                    _SESSION_ID_TEMPLATE % self._session_id_bytes,
                    commands_payload,
                    _BROADSOFT_DOC_END,
                ]
            )
        else:
            command_bytes = command.encode("ISO-8859-1")

            return b"".join(
                [
                    _XML_DECLARATION,
                    _BROADSOFT_DOC_START,
                    _SESSION_ID_TEMPLATE % self._session_id_bytes,
                    command_bytes,
                    _BROADSOFT_DOC_END,
                ]
            )


@attr.s(slots=True, kw_only=True)
class AsyncTCPRequester(BaseRequester):
    """A requester for BroadWorks OCI-P.

    Args:
        host (str): The Broadworks Server IP (e.g adp.broadworks.com)
        port (int): The port of the Broadworks Server, usually 2208 for non tls / 2209 for tls
        config (PoolConfig): The timeout and general pool settings
        tls (bool): Whether to use a secure wrapped socket, recommened: True
        session_id (str): The session_id to send in requests
        logger (Logger): The logger object to retrieve logs and information.
    """

    async def disconnect(self) -> DisconnectResult:
        """Disconnects from the server."""

    async def send_request(self, command: str) -> RequestResult:
        """Sends a request to the server.

        Args:
            command (BroadworksCommand): The command to send to the server.

        Returns:
            Any: The response from the server.
        """

        try:
            async with self._pool.acquire() as conn:
                command_bytes: bytes = self.build_oci_xml(command=command)

                self.logger.debug(f"Sending command to {self.host}: {command}")

                conn.writer.write(command_bytes + b"\n")
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
                            msg=f"Socket read timed out in {self.__class__.__name__}: {e}"
                        )
                        return MErrorSocketTimeout(str(e))

                    if not chunk:
                        break

                    content += chunk

                    if b"</BroadsoftDocument>" in content:
                        break
                return content.rstrip(b"\n").decode("ISO-8859-1")
        except Exception as e:
            self.logger.error(
                msg=f"Failed to send command over {self.__class__.__name__}: {e}"
            )
            return MErrorSendRequestFailed(str(e))

    async def connect():
        pass

    async def send_bulk_request(self, commands: list[str]) -> list[RequestResult]:
        pass
