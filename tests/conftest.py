"""Shared fixtures and helpers for the test suite.

The helpers build canned OCI replies. They let the tests run without a real
BroadWorks server.
"""

from __future__ import annotations

import attrs

from mercury_ocip_fast.session.session import SessionPair


def broadsoft_reply(command_body: str, session_id: str = "sid-1") -> str:
    """Wrap a command body in the BroadsoftDocument reply the server sends."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<BroadsoftDocument protocol="OCI"'
        ' xmlns="C" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f'<sessionId xmlns="">{session_id}</sessionId>'
        f"{command_body}"
        "</BroadsoftDocument>"
    )


def command_xml(xsi_type: str, inner: str = "") -> str:
    """Build one ``<command>`` element with an ``xsi:type`` attribute."""
    return f'<command xsi:type="{xsi_type}">{inner}</command>'


def error_command_xml(summary: str = "Boom", code: str = "4001") -> str:
    """Build one ``ErrorResponse`` command element."""
    return command_xml(
        "c:ErrorResponse",
        f"<summary>{summary}</summary><errorCode>{code}</errorCode>",
    )


@attrs.define(slots=True)
class FakeAtom:
    """A stand-in session atom for the pool and client tests.

    The atom does no network work. It returns a queued reply, or the same
    reply for each send. It tracks its own state, so a test can check the
    pool behaviour.
    """

    session_id: str = "fake-sid"
    reply: str = ""
    replies: list[str] | None = None
    alive: bool = True
    stale: bool = False
    closed: bool = False
    sent: list[str | list[str]] = attrs.field(factory=list)
    _pair: SessionPair | None = None

    async def send(self, payload: str | list[str]) -> str:
        self.sent.append(payload)
        if self.replies:
            return self.replies.pop(0)
        return self.reply

    async def close(self) -> None:
        self.closed = True
        self.alive = False

    def is_alive(self) -> bool:
        return self.alive

    def is_stale(self) -> bool:
        return self.stale

    @property
    def pair(self) -> SessionPair:
        assert self._pair is not None
        return self._pair
