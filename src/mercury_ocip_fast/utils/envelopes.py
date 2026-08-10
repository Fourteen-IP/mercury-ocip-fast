from __future__ import annotations

import xml.etree.ElementTree as ET
from enum import StrEnum
from html import escape

from mercury_ocip_fast.exceptions import MErrorMalformedResponse


class SoapWsdl(StrEnum):
    SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
    BW_NS = "urn:com:broadsoft:webservice"  # request + response namespace
    ARG = "in0"  # request child
    RETURN = "processOCIMessageReturn"  # response child


def build_broadsoft_envelope(commands: str | list[str], session_id: str) -> str:
    """Wrap one or more OCI commands in the BroadsoftDocument envelope BroadWorks expects."""
    payload = "\n".join(commands) if isinstance(commands, list) else commands
    return (
        '<?xml version="1.0" encoding="ISO-8859-1"?>'
        '<BroadsoftDocument protocol="OCI" xmlns="C"'
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f'<sessionId xmlns="">{session_id}</sessionId>'
        f"{payload}"
        "</BroadsoftDocument>"
    )


def wrap_soap(oci_xml: str) -> str:
    """Put the OCI document inside the SOAP envelope, as an escaped string."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<soapenv:Envelope xmlns:soapenv="{SoapWsdl.SOAP_NS}">'
        "<soapenv:Body>"
        f'<processOCIMessage xmlns="{SoapWsdl.BW_NS}">'
        f"<{SoapWsdl.ARG}>{escape(oci_xml)}</{SoapWsdl.ARG}>"
        "</processOCIMessage>"
        "</soapenv:Body>"
        "</soapenv:Envelope>"
    )


def unwrap_soap(response_xml: str) -> str:
    """Pull the OCI reply string back out of the SOAP response envelope.

    The reply document sits as text inside the return element. The XML parser
    unescapes it for us, so its text is the OCI XML we want.
    """

    try:
        root = ET.fromstring(response_xml)
    except ET.ParseError as e:
        raise MErrorMalformedResponse(f"SOAP envelope was not well-formed: {e}") from e
    for elem in root.iter():
        if elem.tag.rsplit("}", 1)[-1] == "processOCIMessageReturn" and elem.text:
            return elem.text
    raise MErrorMalformedResponse("No processOCIMessageReturn in SOAP response")
