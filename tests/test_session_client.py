"""Tests for the multi-tenant ``SessionClient``.

This client keeps no session list. The caller passes its own session to
``command``. So the tests pass a fake atom straight in.
"""

import pytest

from mercury_ocip_fast.commands.commands import (
    AuthenticationRequest,
    AuthenticationResponse,
)
from mercury_ocip_fast.session.session import SessionPair, SOAPSessionSettings
from mercury_ocip_fast.session.soap_session import SOAPSessionAtom
from mercury_ocip_fast.session_client import SessionClient

from conftest import FakeAtom, broadsoft_reply, command_xml


def _auth(user: str) -> str:
    return command_xml("c:AuthenticationResponse", f"<userId>{user}</userId>")


async def build_client() -> SessionClient:
    client = SessionClient(
        host="https://host/webservice",
        atom_type=SOAPSessionAtom,
        session_config=SOAPSessionSettings(),
    )
    return await client._async_setup()


class TestCommand:
    async def test_single_command(self):
        client = await build_client()
        atom = FakeAtom(reply=broadsoft_reply(_auth("admin")))
        result = await client.command(
            atom, AuthenticationRequest(user_id="admin"), response_type=AuthenticationResponse
        )
        assert isinstance(result, AuthenticationResponse)
        assert result.user_id == "admin"

    async def test_batch(self):
        client = await build_client()
        atom = FakeAtom(reply=broadsoft_reply(_auth("a") + _auth("b")))
        result = await client.command(
            atom,
            [AuthenticationRequest(user_id="admin"), AuthenticationRequest(user_id="admin")],
            response_type=AuthenticationResponse,
        )
        assert [r.user_id for r in result] == ["a", "b"]

    async def test_single_item_batch(self):
        # A one-item batch reply is a single object. The requester wraps it in
        # a list, so the batch path returns a list. See the fix in
        # Requester.send.
        client = await build_client()
        atom = FakeAtom(reply=broadsoft_reply(_auth("solo")))
        result = await client.command(
            atom, [AuthenticationRequest(user_id="admin")], response_type=AuthenticationResponse
        )
        assert [r.user_id for r in result] == ["solo"]


class TestSessionLifecycle:
    async def test_close_delegates_to_session(self):
        client = await build_client()
        atom = FakeAtom()
        await client.close(atom)
        assert atom.closed is True

    async def test_open_calls_login_factory(self, monkeypatch):
        client = await build_client()
        opened = FakeAtom(session_id="opened")

        async def fake_login(self, username, password):
            assert (username, password) == ("u", "p")
            return opened

        # The client uses slots, so patch the method on the class.
        monkeypatch.setattr(SessionClient, "_login_factory", fake_login)
        result = await client.open("u", "p")
        assert result is opened

    async def test_resume_calls_resume_factory(self, monkeypatch):
        client = await build_client()
        resumed = FakeAtom(session_id="resumed")
        pair = SessionPair(jsessionid="J", session_id="S")

        async def fake_resume(self, passed_pair):
            assert passed_pair is pair
            return resumed

        monkeypatch.setattr(SessionClient, "_resume_factory", fake_resume)
        result = await client.resume(pair)
        assert result is resumed

    async def test_aenter_owns_no_sessions(self):
        # The client owns no sessions, so leaving the block closes nothing.
        client = SessionClient(
            host="https://host/webservice",
            atom_type=SOAPSessionAtom,
            session_config=SOAPSessionSettings(),
        )
        async with client as ready:
            assert ready is client
