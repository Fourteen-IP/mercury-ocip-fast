"""Tests for the OCI ``Parser`` and the ``OCIType`` base class.

These tests use real generated command classes. They check the round trip
from a class to XML and back, and the parse of a server reply into a dict.
"""

import pytest

from mercury_ocip_fast.commands.base_command import (
    ErrorResponse,
    OCITable,
    OCITableRow,
    OCIType,
)
from mercury_ocip_fast.commands.commands import (
    AuthenticationRequest,
    AuthenticationResponse,
    LoginRequest22V5,
)
from mercury_ocip_fast.utils.parser import Parser


class TestToXml:
    def test_emits_class_name_and_field(self):
        xml = AuthenticationRequest(user_id="admin").to_xml()
        assert 'xsi:type="AuthenticationRequest"' in xml
        # The field alias turns ``user_id`` into ``userId``.
        assert "<userId>admin</userId>" in xml

    def test_none_fields_are_dropped(self):
        # A login request has a ``userId`` and a ``password``. A field that is
        # None does not go into the body.
        xml = LoginRequest22V5(user_id="admin").to_xml()
        assert "<userId>admin</userId>" in xml
        assert "password" not in xml


class TestClassXmlRoundTrip:
    def test_authentication_request(self):
        original = AuthenticationRequest(user_id="admin")
        restored = AuthenticationRequest.from_xml(original.to_xml())
        assert restored.user_id == "admin"


class TestToDictFromXml:
    def test_reads_command_and_attributes(self):
        xml = (
            '<BroadsoftDocument xmlns:xsi='
            '"http://www.w3.org/2001/XMLSchema-instance">'
            '<command xsi:type="AuthenticationResponse">'
            "<userId>admin</userId><nonce>12345</nonce>"
            "</command></BroadsoftDocument>"
        )
        result = Parser.to_dict_from_xml(xml)
        command = result["command"]
        assert command["userId"] == "admin"
        assert command["nonce"] == "12345"
        # The parser lifts the ``xsi:type`` into an XMLSchema-instance key.
        xsi_key = "{http://www.w3.org/2001/XMLSchema-instance}type"
        assert command["attributes"][xsi_key] == "AuthenticationResponse"

    def test_non_string_input_returns_empty(self):
        assert Parser.to_dict_from_xml(None) == {}  # type: ignore[arg-type]


class TestAuthenticationResponseFromDict:
    def test_parses_fields(self):
        payload = {
            "AuthenticationResponse": {
                "userId": "admin",
                "nonce": "abcdef",
                "passwordAlgorithm": "MD5",
            }
        }
        resp = AuthenticationResponse.from_dict(payload)
        assert resp.user_id == "admin"
        assert resp.nonce == "abcdef"


class TestErrorResponse:
    def test_from_dict_reads_alias_fields(self):
        payload = {
            "attributes": {
                "{http://www.w3.org/2001/XMLSchema-instance}type": "ErrorResponse"
            },
            "summary": "Login failed",
            "errorCode": 4001,
        }
        err = ErrorResponse.from_dict(payload)
        assert err.summary == "Login failed"
        assert err.error_code == 4001


class TestOCITypeInit:
    def test_unknown_field_raises(self):
        with pytest.raises(ValueError):
            AuthenticationRequest.__bases__  # sanity: class exists
            OCIType(not_a_field="x")

    def test_field_aliases_for_dataclass(self):
        aliases = AuthenticationRequest(user_id="a").get_field_aliases()
        assert aliases["user_id"] == "userId"

    def test_field_aliases_empty_for_plain_type(self):
        assert OCIType().get_field_aliases() == {}


class TestOCITable:
    def test_to_dict_maps_headings_to_rows(self):
        table = OCITable(
            col_heading=["User Id", "Last Name"],
            row=[
                OCITableRow(col=["a@x.com", "Smith"]),
                OCITableRow(col=["b@x.com", "Jones"]),
            ],
        )
        result = table.to_dict()
        assert result == [
            {"user_id": "a@x.com", "last_name": "Smith"},
            {"user_id": "b@x.com", "last_name": "Jones"},
        ]
