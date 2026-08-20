from __future__ import annotations

import asyncio
import logging
import ssl
import time
import uuid
from ssl import SSLContext

import attrs

from mercury_ocip_fast.exceptions import (
    MErrorSocketDropped,
    MErrorSocketInitialisation,
    MErrorSocketTimeout,
)
from mercury_ocip_fast.session.session import TCPSessionSettings
from mercury_ocip_fast.utils.endpoints import split_host_port
from mercury_ocip_fast.utils.envelopes import build_broadsoft_envelope

logger = logging.getLogger(__name__)


@attrs.define(kw_only=True, slots=True)
class TCPSessionAtom:
    """A TCP session for BroadWorks.

    The transport is an asyncio stream. The stream has a reader and a writer.
    The session sends the OCI-P document on the raw socket.

    Attributes:
        reader: The stream reader for the socket.
        writer: The stream writer for the socket.
        ssl_context: The TLS context for the socket, or None for no TLS.
        settings: The timeouts and the read size for the socket.
        session_id: The OCI-P session id for each message body.
    """

    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    ssl_context: SSLContext | None
    settings: TCPSessionSettings = attrs.field(default=TCPSessionSettings())
    session_id: str = attrs.field(factory=lambda: str(uuid.uuid4()))
    created_at: float = attrs.field(factory=time.monotonic)
    last_used: float = attrs.field(factory=time.monotonic)

    @classmethod
    async def open(
        cls,
        endpoint: str,
        port: int | None = None,
        *,
        settings: TCPSessionSettings,
        verify_ssl: bool = True,
    ) -> TCPSessionAtom:
        """Open a new TCP session. The session is not logged in.

        This method opens a socket to the server. It waits for the
        connection. If TLS is on, it also does the TLS handshake.

        Args:
            endpoint: The address of the server. This is a host, a
                ``host:port`` pair, or a ``scheme://host:port`` URL. An IPv6
                host must be in brackets, for example ``[::1]:2209``.
            port: The TCP port of the server. This argument has priority over
                a port in the endpoint. The default is 2209 (TLS).
            settings: The timeouts and the read size for the socket.
            verify_ssl: If true, use TLS and verify the server certificate.

        Returns:
            A new TCP session with an open socket.

        Raises:
            MErrorSocketTimeout: If the connection does not open in time.
            MErrorSocketInitialisation: If the endpoint has no host, or the
                socket cannot open.
        """
        ssl_context = ssl.create_default_context() if verify_ssl else None

        try:
            host, tcp_port = split_host_port(endpoint, port, default=2209)
        except ValueError as e:
            raise MErrorSocketInitialisation(str(e)) from e

        logger.debug(
            "Open a TCP session to %s:%d, connect timeout %ss",
            host,
            tcp_port,
            settings.connect_timeout,
        )

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, tcp_port, ssl=ssl_context),
                timeout=settings.connect_timeout,
            )
        except TimeoutError as e:
            logger.warning(
                "Cannot open a TCP session to %s:%d: no connection in %ss",
                host,
                tcp_port,
                settings.connect_timeout,
            )
            raise MErrorSocketTimeout(
                f"Connection timeout after {settings.connect_timeout}s"
            ) from e
        except OSError as e:
            logger.warning("Cannot open a TCP session to %s:%d: %s", host, tcp_port, e)
            raise MErrorSocketInitialisation(f"Connection failed: {e}") from e

        return cls(
            reader=reader, writer=writer, ssl_context=ssl_context, settings=settings
        )

    async def send(self, payload: str | list[str]) -> str:
        """Send one envelope to the server and return one reply.

        This method builds the BroadsoftDocument from the payload. It
        writes the document on the socket. It then reads the reply in
        chunks. It stops the read at the end tag of the document.

        Do not write the payload or the reply to a log. They can hold
        secrets, for example a password in a login command.

        Args:
            payload: One OCI command, or a list of OCI commands.

        Returns:
            The OCI reply as a string.

        Raises:
            MErrorSocketDropped: If the connection stops during the write
                or the read.
            MErrorSocketTimeout: If a read does not complete in time.
        """
        peer = self.writer.get_extra_info("peername")
        oci_xml = build_broadsoft_envelope(payload, self.session_id).encode()

        logger.debug("Send %d bytes to %s", len(oci_xml), peer)

        try:
            self.writer.writelines([oci_xml, b"\n"])
            await self.writer.drain()
        except (ConnectionResetError, BrokenPipeError, RuntimeError) as e:
            logger.warning(
                "Lost the TCP connection to %s during the write: %s", peer, e
            )
            raise MErrorSocketDropped(str(e)) from e

        content = bytearray()

        while True:
            try:
                chunk: bytes = await asyncio.wait_for(
                    self.reader.read(self.settings.read_chunk_size),
                    timeout=self.settings.read_timeout,
                )
            except TimeoutError as e:
                logger.warning(
                    "No TCP reply from %s in %ss; %d bytes read so far",
                    peer,
                    self.settings.read_timeout,
                    len(content),
                )
                raise MErrorSocketTimeout(
                    f"Read timeout after {self.settings.read_timeout}s: {e}"
                ) from e
            except (ConnectionResetError, BrokenPipeError, RuntimeError) as e:
                logger.warning(
                    "Lost the TCP connection to %s during the read after %d bytes: %s",
                    peer,
                    len(content),
                    e,
                )
                raise MErrorSocketDropped(f"Connection failed: {e}") from e

            if not chunk:
                break

            content.extend(chunk)

            if b"</BroadsoftDocument>" in content:
                break

        response = content.rstrip(b"\n").decode("iso-8859-1")

        logger.debug("Receive %d bytes from %s", len(content), peer)

        return response

    async def close(self) -> None:
        """Close the socket and release the transport.

        The method is safe to call more than once.
        """
        self.writer.close()
        try:
            await asyncio.wait_for(self.writer.wait_closed(), timeout=1.0)
        except (TimeoutError, Exception) as e:  # noqa: BLE001
            logger.debug("TLS close did not finish cleanly (harmless): %s", e)

    def is_alive(self) -> bool:
        """Tell if the session is still connected.

        The session is not healthy if the writer is in a close. The session
        is not healthy if the reader is at the end of the stream.
        """
        if self.writer.is_closing():
            return False

        try:
            if self.reader.at_eof():
                return False
        except Exception:  # noqa: BLE001
            return False

        return True

    def is_stale(self) -> bool:
        """Tell if the session is past its time to live."""
        return (time.monotonic() - self.created_at) > self.settings.max_ttl_seconds

    def touch(self) -> None:
        """Mark the session just used."""
        self.last_used = time.monotonic()
