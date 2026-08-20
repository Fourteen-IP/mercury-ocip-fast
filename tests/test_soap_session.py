"""Tests for ``SOAPSessionAtom``.

The tests use an httpx ``MockTransport``. The transport answers the POST in
the test, so no real server runs. The atom is built by hand, so the mock
transport goes into its httpx client.
"""

from html import escape

import httpx
import pytest

from mercury_ocip_fast.exceptions import (
    MErrorHttpInitialisation,
    MErrorHttpStatus,
    MErrorMissingSessionIdentity,
)
from mercury_ocip_fast.session.session import SessionPair, SOAPSessionSettings
from mercury_ocip_fast.session.soap_session import SOAPSessionAtom

ENDPOINT = "https://host/webservice/services/ProvisioningService"


def soap_return(oci: str, set_cookie: str | None = None) -> httpx.Response:
    """Build a SOAP response that carries the OCI reply."""
    body = (
        '<soapenv:Envelope xmlns:soapenv='
        '"http://schemas.xmlsoap.org/soap/envelope/"><soapenv:Body>'
        "<processOCIMessageResponse>"
        f"<processOCIMessageReturn>{escape(oci)}</processOCIMessageReturn>"
        "</processOCIMessageResponse></soapenv:Body></soapenv:Envelope>"
    )
    headers = {"Set-Cookie": set_cookie} if set_cookie else {}
    return httpx.Response(200, text=body, headers=headers)


def make_atom(handler, **atom_kwargs) -> SOAPSessionAtom:
    return SOAPSessionAtom(
        endpoint=ENDPOINT,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        settings=SOAPSessionSettings(),
        **atom_kwargs,
    )


class TestSend:
    async def test_send_returns_unwrapped_reply(self):
        oci = "<BroadsoftDocument>ok</BroadsoftDocument>"

        def handler(request: httpx.Request) -> httpx.Response:
            # The atom wraps the body in a SOAP envelope with the session id.
            assert b"processOCIMessage" in request.content
            return soap_return(oci)

        atom = make_atom(handler)
        assert await atom.send("<command/>") == oci
        await atom.close()

    async def test_login_stores_cookie_for_pair(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return soap_return(
                "<BroadsoftDocument/>", set_cookie="JSESSIONID=ABC123; Path=/"
            )

        atom = make_atom(handler, session_id="oci-1")
        await atom.send("<command/>")
        assert atom.jsessionid == "ABC123"
        assert atom.pair == SessionPair(jsessionid="ABC123", session_id="oci-1")
        await atom.close()

    async def test_http_error_maps_to_status(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        atom = make_atom(handler)
        with pytest.raises(MErrorHttpStatus) as info:
            await atom.send("<command/>")
        assert info.value.status == 500
        await atom.close()


class TestPair:
    async def test_pair_before_login_raises(self):
        atom = make_atom(lambda r: soap_return(""))
        with pytest.raises(MErrorMissingSessionIdentity):
            _ = atom.pair
        await atom.close()


class TestResume:
    async def test_resume_sets_cookie_and_id(self):
        pair = SessionPair(jsessionid="COOKIE9", session_id="oci-9")
        atom = await SOAPSessionAtom.resume(
            ENDPOINT, pair, settings=SOAPSessionSettings()
        )
        assert atom.session_id == "oci-9"
        assert atom.jsessionid == "COOKIE9"
        assert atom.pair == pair
        await atom.close()


class TestOpen:
    async def test_open_builds_a_live_session(self):
        atom = await SOAPSessionAtom.open(ENDPOINT, settings=SOAPSessionSettings())
        assert atom.is_alive()
        assert atom.endpoint == ENDPOINT
        await atom.close()
        assert atom.is_alive() is False

    async def test_open_bad_url_raises(self):
        # A URL with no host cannot take an override port.
        with pytest.raises(MErrorHttpInitialisation):
            await SOAPSessionAtom.open(
                "not-a-url", port=8443, settings=SOAPSessionSettings()
            )


class TestStale:
    async def test_fresh_session_is_not_stale(self):
        atom = make_atom(lambda r: soap_return(""))
        assert atom.is_stale() is False
        await atom.close()

    async def test_zero_ttl_is_stale(self):
        atom = SOAPSessionAtom(
            endpoint=ENDPOINT,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r: soap_return(""))
            ),
            settings=SOAPSessionSettings(max_ttl_seconds=0),
        )
        assert atom.is_stale() is True
        await atom.close()
