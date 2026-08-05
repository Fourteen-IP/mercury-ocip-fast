from __future__ import annotations

import asyncio
import ssl
import uuid
from ssl import SSLContext

import attrs

from mercury_ocip_fast_v2.exceptions import (
    MErrorSocketDropped,
    MErrorSocketInitialisation,
    MErrorSocketMalformedPayload,
    MErrorSocketTimeout,
)
from mercury_ocip_fast_v2.session.session import SessionAtom, TCPSessionSettings
from mercury_ocip_fast_v2.utils.envelopes import build_broadsoft_envelope


@attrs.define(kw_only=True, slots=True)
class TCPSessionAtom(SessionAtom):
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    ssl_context: SSLContext | None
    settings: TCPSessionSettings = attrs.field(default=TCPSessionSettings())
    session_id: str = attrs.field(factory=lambda: str(uuid.uuid4()))

    @classmethod
    async def open(
        cls,
        endpoint: str,
        port: int,
        *,
        settings: TCPSessionSettings,
        ssl_verify: bool = True,
    ) -> TCPSessionAtom:
        """Make a fresh logged-out TCP Session with a new client."""
        ssl_context = ssl.create_default_context() if ssl_verify else None

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(endpoint, port, ssl=ssl_context),
                timeout=settings.connect_timeout,
            )
        except asyncio.TimeoutError as e:
            raise MErrorSocketTimeout(
                f"Connection timeout after {settings.connect_timeout}s"
            ) from e
        except OSError as e:
            raise MErrorSocketInitialisation(f"Connection failed: {e}") from e

        return cls(
            reader=reader, writer=writer, ssl_context=ssl_context, settings=settings
        )

    async def send(self, payload: str | list[str]) -> str:
        """Send the payload to the server and return one reply."""
        oci_xml = build_broadsoft_envelope(payload, self.session_id).encode()

        try:
            self.writer.writelines([oci_xml, b"\n"])
            await self.writer.drain()
        except (ConnectionResetError, BrokenPipeError, RuntimeError) as e:
            raise MErrorSocketDropped(str(e)) from e

        content = bytearray()

        while True:
            try:
                chunk: bytes = await asyncio.wait_for(
                    self.reader.read(self.settings.read_chunk_size),
                    timeout=self.settings.read_timeout,
                )
            except asyncio.TimeoutError as e:
                raise MErrorSocketTimeout(
                    f"Connection timeout after {self.settings.connect_timeout}s: {e}"
                ) from e
            except (ConnectionResetError, BrokenPipeError, RuntimeError) as e:
                raise MErrorSocketDropped(f"Connection failed: {e}") from e

            if not chunk:
                break

            content.extend(chunk)

            if b"</BroadsoftDocument>" in content:
                break

        try:
            response = content.rstrip(b"\n").decode()
        except UnicodeDecodeError as e:
            raise MErrorSocketMalformedPayload(
                f"Broadworks returned with a byte response which could not be decoded: {e}"
            ) from e

        return response

    async def close(self) -> None:
        """Close the session and let go of its transport."""
        try:
            self.writer.close()
        except Exception:
            pass

    def is_healthy(self) -> bool:
        """Is the session still usable?"""
        if self.writer.is_closing():
            return False

        try:
            if self.reader.at_eof():
                return False
        except Exception:
            return False

        return True
