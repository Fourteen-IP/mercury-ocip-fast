"""Tests for the SOAP and BroadsoftDocument envelope helpers."""

from html import escape, unescape

import pytest

from mercury_ocip_fast.exceptions import MErrorMalformedResponse
from mercury_ocip_fast.utils.envelopes import (
    build_broadsoft_envelope,
    unwrap_soap,
    wrap_soap,
)


class TestBuildBroadsoftEnvelope:
    def test_single_command(self):
        result = build_broadsoft_envelope("<command/>", "sid-1")
        assert "<sessionId xmlns=\"\">sid-1</sessionId>" in result
        assert "<command/>" in result
        assert result.startswith("<?xml")
        assert result.endswith("</BroadsoftDocument>")

    def test_list_joins_commands(self):
        result = build_broadsoft_envelope(["<a/>", "<b/>"], "sid-1")
        assert "<a/>\n<b/>" in result


class TestSoapRoundTrip:
    def test_wrap_then_unwrap(self):
        oci = build_broadsoft_envelope("<command/>", "sid-1")
        wrapped = wrap_soap(oci)
        # The OCI document is escaped inside the SOAP body.
        assert escape(oci) in wrapped
        # A server echoes the same content inside processOCIMessageReturn.
        server_reply = (
            '<soapenv:Envelope xmlns:soapenv='
            '"http://schemas.xmlsoap.org/soap/envelope/"><soapenv:Body>'
            '<processOCIMessageResponse>'
            f"<processOCIMessageReturn>{escape(oci)}</processOCIMessageReturn>"
            "</processOCIMessageResponse></soapenv:Body></soapenv:Envelope>"
        )
        assert unwrap_soap(server_reply) == oci


class TestUnwrapSoap:
    def test_missing_return_element_raises(self):
        reply = (
            '<soapenv:Envelope xmlns:soapenv='
            '"http://schemas.xmlsoap.org/soap/envelope/">'
            "<soapenv:Body></soapenv:Body></soapenv:Envelope>"
        )
        with pytest.raises(MErrorMalformedResponse):
            unwrap_soap(reply)

    def test_not_well_formed_raises(self):
        with pytest.raises(MErrorMalformedResponse):
            unwrap_soap("<not-closed>")

    def test_unescapes_content(self):
        inner = escape("<BroadsoftDocument>x</BroadsoftDocument>")
        reply = (
            "<Envelope><Body><processOCIMessageReturn>"
            f"{inner}</processOCIMessageReturn></Body></Envelope>"
        )
        assert unwrap_soap(reply) == unescape(inner)
