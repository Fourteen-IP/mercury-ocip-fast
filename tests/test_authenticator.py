"""Tests for the ``Authenticator``: the two login flows.

The tests use a fake session that returns canned replies in order. So the
login runs without a real server.
"""

import hashlib

import pytest

from mercury_ocip_fast.authenticator import Authenticator
from mercury_ocip_fast.exceptions import MErrorLogin
from mercury_ocip_fast.requester import Requester

from conftest import FakeAtom, broadsoft_reply, command_xml, error_command_xml


def auth_response(nonce: str = "nonce-1") -> str:
    return broadsoft_reply(
        command_xml(
            "c:AuthenticationResponse",
            f"<userId>admin</userId><nonce>{nonce}</nonce>"
            "<passwordAlgorithm>MD5</passwordAlgorithm>",
        )
    )


def login14_response() -> str:
    return broadsoft_reply(command_xml("c:LoginResponse14sp4", ""))


def login22_response() -> str:
    return broadsoft_reply(
        command_xml(
            "c:LoginResponse22V5",
            "<loginType>System</loginType><locale>en_US</locale>"
            "<encoding>ISO-8859-1</encoding><isEnterprise>true</isEnterprise>"
            "<userDomain>example.com</userDomain>",
        )
    )


def make_auth() -> Authenticator:
    return Authenticator(username="admin", password="secret", requester=Requester())


class TestSign:
    def test_sign_matches_reference_hash(self):
        auth = make_auth()
        nonce = "abc"
        expected = hashlib.md5(
            f"{nonce}:{hashlib.sha1(b'secret').hexdigest().lower()}".encode()
        ).hexdigest().lower()
        assert auth._sign(nonce) == expected


class TestGenericLogin:
    async def test_success(self):
        auth = make_auth()
        atom = FakeAtom(replies=[login22_response()])
        resp = await auth.generic_login(atom)
        assert resp.login_type == "System"
        # The plain-text flow sends exactly one command.
        assert len(atom.sent) == 1

    async def test_server_error_raises_login(self):
        auth = make_auth()
        atom = FakeAtom(replies=[broadsoft_reply(error_command_xml("nope"))])
        with pytest.raises(MErrorLogin):
            await auth.generic_login(atom)


class TestEncryptedLogin:
    async def test_two_step_flow(self):
        auth = make_auth()
        atom = FakeAtom(replies=[auth_response("nonce-1"), login14_response()])
        await auth.encrypted_login(atom)
        # The flow sends an AuthenticationRequest then a signed LoginRequest.
        assert len(atom.sent) == 2
        signed = auth._sign("nonce-1")
        assert signed in atom.sent[1]

    async def test_auth_error_raises_login(self):
        auth = make_auth()
        atom = FakeAtom(replies=[broadsoft_reply(error_command_xml("bad user"))])
        with pytest.raises(MErrorLogin):
            await auth.encrypted_login(atom)
