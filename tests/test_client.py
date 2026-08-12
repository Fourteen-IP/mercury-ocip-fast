"""Tests for the pooled ``Client``.

The tests replace the pool and the requester with fakes, so no network
runs. They check the single-command path, the batch path, and close.
"""

from mercury_ocip_fast.client import Client
from mercury_ocip_fast.commands.commands import (
    AuthenticationRequest,
    AuthenticationResponse,
)
from mercury_ocip_fast.pool.session_pool import SessionPoolSettings
from mercury_ocip_fast.pool.session_pool import SessionPool
from mercury_ocip_fast.requester import Requester
from mercury_ocip_fast.session.session import SOAPSessionSettings
from mercury_ocip_fast.session.soap_session import SOAPSessionAtom

from conftest import FakeAtom, broadsoft_reply, command_xml


def _auth(user: str) -> str:
    return command_xml("c:AuthenticationResponse", f"<userId>{user}</userId>")


def build_client(atom: FakeAtom) -> Client:
    """Make a client whose pool always hands out the given fake atom."""

    async def factory() -> FakeAtom:
        return atom

    client = Client(
        host="host",
        username="admin",
        password="secret",
        atom_type=SOAPSessionAtom,
        session_config=SOAPSessionSettings(),
        pool_config=SessionPoolSettings(max_size=2),
    )
    # Wire the internals by hand, so the client never opens a real session.
    client._requester = Requester()
    client._pool = SessionPool(default_factory=factory, pool_settings=client.pool_config)
    return client


class TestSingleCommand:
    async def test_returns_one_response(self):
        atom = FakeAtom(reply=broadsoft_reply(_auth("admin")))
        client = build_client(atom)
        result = await client.command(
            AuthenticationRequest(user_id="admin"), response_type=AuthenticationResponse
        )
        assert isinstance(result, AuthenticationResponse)
        assert result.user_id == "admin"
        await client.close()


class TestBatch:
    async def test_full_batch_returns_a_list(self):
        # Two commands make a reply with two command elements.
        atom = FakeAtom(reply=broadsoft_reply(_auth("a") + _auth("b")))
        client = build_client(atom)
        requests = [AuthenticationRequest(user_id="admin"), AuthenticationRequest(user_id="admin")]
        result = await client.command(requests, response_type=AuthenticationResponse)
        assert isinstance(result, list)
        assert [r.user_id for r in result] == ["a", "b"]
        await client.close()

    async def test_single_item_batch(self):
        # A batch whose reply holds exactly one command. The requester wraps
        # the lone result in a list, so extend does not fail. See the fix in
        # Requester.send.
        atom = FakeAtom(reply=broadsoft_reply(_auth("solo")))
        client = build_client(atom)
        result = await client.command(
            [AuthenticationRequest(user_id="admin")], response_type=AuthenticationResponse
        )
        assert [r.user_id for r in result] == ["solo"]
        await client.close()

    async def test_sixteen_items_cross_the_batch_boundary(self):
        # 16 requests split into a group of 15 and a group of 1. The first
        # reply is a list; the second reply is a single command. Both must
        # extend the results, so the total stays 16.
        first = broadsoft_reply("".join(_auth(f"u{i}") for i in range(15)))
        second = broadsoft_reply(_auth("u15"))
        atom = FakeAtom(replies=[first, second])
        client = build_client(atom)
        requests = [AuthenticationRequest(user_id=f"u{i}") for i in range(16)]
        result = await client.command(requests, response_type=AuthenticationResponse)
        assert isinstance(result, list)
        assert [r.user_id for r in result] == [f"u{i}" for i in range(16)]
        await client.close()


class TestClose:
    async def test_close_is_safe_before_setup(self):
        client = Client(
            host="host",
            username="admin",
            password="secret",
            atom_type=SOAPSessionAtom,
            session_config=SOAPSessionSettings(),
            pool_config=SessionPoolSettings(),
        )
        # No pool yet; close must not raise.
        await client.close()

    async def test_context_manager_sets_up_and_closes(self):
        # ``async with`` runs the setup and builds a real (empty) pool.
        client = Client(
            host="host",
            username="admin",
            password="secret",
            atom_type=SOAPSessionAtom,
            session_config=SOAPSessionSettings(),
            pool_config=SessionPoolSettings(),
        )
        async with client as ready:
            assert ready._pool is not None
