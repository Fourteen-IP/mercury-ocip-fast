from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Self, TypeVar, runtime_checkable


@dataclass(frozen=True, slots=True, kw_only=True)
class SOAPSessionSettings:
    """Timeouts for a SOAP session's httpx client, in seconds."""

    connect_timeout: float = 30.0
    read_timeout: float = 30.0
    write_timeout: float = 30.0


@dataclass(frozen=True, slots=True, kw_only=True)
class TCPSessionSettings:
    """Timeouts and config for a TCP session"""

    connect_timeout: int = field(default=30)
    read_timeout: int = field(default=30)
    read_chunk_size: int = field(default=8192)


@dataclass(frozen=True, slots=True)
class SessionPair:
    """One logged-in BroadWorks SOAP identity, held as a single value.

    BroadWorks ties a login to two values at the same time: the JSESSIONID
    cookie and the OCI-P session id in the message body. The server rejects a
    request that carries one value without the other. So keep the two together
    as one unit, and pass this object instead of two loose strings.

    Store this pair to resume a session later. Treat both values as secrets.
    Anyone who holds the pair can send commands as the user.

    Attributes:
        jsessionid: The JSESSIONID cookie value from the login.
        session_id: The OCI-P session id paired with that cookie.
    """

    jsessionid: str
    session_id: str


@runtime_checkable
class SessionAtom[S: (TCPSessionSettings, SOAPSessionSettings)](Protocol):
    """The smallest unit of transport: a SessionAtom holds a session pair and can
    send payloads over that session.
    """

    session_id: str

    @classmethod
    async def open(
        cls,
        endpoint: str,
        port: int | None = None,
        *,
        settings: S,
        verify_ssl: bool = True,
        **kwargs,
    ) -> Self: ...

    async def send(self, payload: str | list[str]) -> str:
        """Send one envelope (one or more commands) and return one reply."""
        ...

    async def close(self) -> None:
        """Close the session and let go of its transport."""
        ...

    def is_healthy(self) -> bool:
        """Is the session still usable?"""
        ...


@runtime_checkable
class DetatchableSessionAtom(SessionAtom, Protocol):
    @classmethod
    async def resume(
        cls,
        endpoint: str,
        pair: SessionPair,
        *,
        settings: TCPSessionSettings | SOAPSessionSettings,
        verify_ssl: bool = True,
    ) -> Self: ...

    @property
    def pair(self) -> SessionPair: ...
