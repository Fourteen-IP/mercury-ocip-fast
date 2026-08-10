"""Tests for the ``Requester``: send a payload and parse the reply."""

import pytest

from mercury_ocip_fast.commands.commands import AuthenticationResponse
from mercury_ocip_fast.exceptions import MErrorMalformedResponse, MErrorResponse
from mercury_ocip_fast.requester import Requester

from conftest import broadsoft_reply, command_xml, error_command_xml

AUTH_INNER = "<userId>admin</userId><nonce>xyz</nonce>"


def _auth_command(user: str = "admin") -> str:
    return command_xml("c:AuthenticationResponse", f"<userId>{user}</userId>")


class TestParseResponse:
    def test_single_command_returns_one_object(self):
        reply = broadsoft_reply(command_xml("c:AuthenticationResponse", AUTH_INNER))
        result = Requester().parse_response(reply, AuthenticationResponse)
        assert isinstance(result, AuthenticationResponse)
        assert result.user_id == "admin"

    def test_many_commands_return_a_list(self):
        reply = broadsoft_reply(_auth_command("a") + _auth_command("b"))
        result = Requester().parse_response(reply, AuthenticationResponse)
        assert isinstance(result, list)
        assert [r.user_id for r in result] == ["a", "b"]

    def test_error_response_raises(self):
        reply = broadsoft_reply(error_command_xml("Login failed"))
        with pytest.raises(MErrorResponse):
            Requester().parse_response(reply, AuthenticationResponse)

    def test_missing_command_raises_malformed(self):
        reply = (
            '<BroadsoftDocument xmlns:xsi='
            '"http://www.w3.org/2001/XMLSchema-instance">'
            "</BroadsoftDocument>"
        )
        with pytest.raises(MErrorMalformedResponse):
            Requester().parse_response(reply, AuthenticationResponse)

    def test_missing_xsi_type_raises_malformed(self):
        reply = broadsoft_reply("<command><userId>x</userId></command>")
        with pytest.raises(MErrorMalformedResponse):
            Requester().parse_response(reply, AuthenticationResponse)


class TestSend:
    async def test_send_uses_the_session(self):
        from conftest import FakeAtom

        reply = broadsoft_reply(command_xml("c:AuthenticationResponse", AUTH_INNER))
        atom = FakeAtom(reply=reply)
        result = await Requester().send(
            payload="<command/>",
            response_type=AuthenticationResponse,
            session=atom,
        )
        assert atom.sent == ["<command/>"]
        assert isinstance(result, AuthenticationResponse)

    async def test_list_payload_of_one_returns_a_list(self):
        # A list payload always gives a list back, even when the reply holds a
        # single command. This keeps send in step with its list[str] overload.
        from conftest import FakeAtom

        reply = broadsoft_reply(command_xml("c:AuthenticationResponse", AUTH_INNER))
        atom = FakeAtom(reply=reply)
        result = await Requester().send(
            payload=["<command/>"],
            response_type=AuthenticationResponse,
            session=atom,
        )
        assert isinstance(result, list)
        assert len(result) == 1

    async def test_str_payload_returns_a_single_object(self):
        from conftest import FakeAtom

        reply = broadsoft_reply(command_xml("c:AuthenticationResponse", AUTH_INNER))
        result = await Requester().send(
            payload="<command/>",
            response_type=AuthenticationResponse,
            session=FakeAtom(reply=reply),
        )
        assert not isinstance(result, list)
