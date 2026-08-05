from __future__ import annotations

import asyncio
import uuid

import attrs

from mercury_ocip_fast_v2.session.session import SessionAtom


@attrs.define(kw_only=True, slots=True)
class TCPSessionAtom(SessionAtom):
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    session_id: str = attrs.field(factory=lambda: str(uuid.uuid4()))

    @classmethod
    def open(cls, endpoint: str, port: str, *, ssl: bool = True) -> TCPSessionAtom:
        """Make a fresh TCP Session with a new client."""
        return cls()

    async def send(self, payload: str | list[str]) -> str:
        """Send one envelope (one or more commands) and return one reply."""

    async def close(self) -> None:
        """Close the session and let go of its transport."""
        try:
            self.writer.close()
        except Exception:
            pass

    def is_healthy(self) -> bool:
        """Is the session still usable?"""
