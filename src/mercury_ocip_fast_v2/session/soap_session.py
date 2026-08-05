from __future__ import annotations

import uuid

import attrs
import httpx

from mercury_ocip_fast_v2.exceptions import MErrorMissingSessionIdentity
from mercury_ocip_fast_v2.session.session import SessionAtom, SessionPair
from mercury_ocip_fast_v2.utils.envelopes import (
    build_broadsoft_envelope,
    unwrap_soap,
    wrap_soap,
)


@attrs.define(kw_only=True, slots=True)
class SOAPSessionAtom(SessionAtom):
    """A whole SOAP session: a httpx client, and login metadata.

    The session owns its transport (an httpx client with its own cookie jar)
    and its identity. ``pair`` contains the session id's for login, and by default
    will contain a fresh uuid: ``session_id``.

    Attributes:
        endpoint: The SOAP service URL.
        http_client: The httpx client that holds this session's cookie jar.
        pair: The session identity, set once the session is logged in.
    """

    endpoint: str
    http_client: httpx.AsyncClient
    session_id: str = attrs.field(factory=lambda: str(uuid.uuid4()))

    @classmethod
    def open(cls, endpoint: str, *, verify_ssl: bool = True) -> SOAPSessionAtom:
        """Make a fresh, logged-out session with its own httpx client."""
        return cls(endpoint=endpoint, http_client=httpx.AsyncClient(verify=verify_ssl))

    @classmethod
    def resume(
        cls, endpoint: str, pair: SessionPair, *, verify_ssl: bool = True
    ) -> SOAPSessionAtom:
        """Make a session that adopts a given session pair."""
        session = cls.open(endpoint, verify_ssl=verify_ssl)
        session.http_client.cookies.set("JSESSIONID", pair.jsessionid)
        session.session_id = pair.session_id
        return session

    @property
    def jsessionid(self) -> str | None:
        """This session's JSESSIONID cookie, None if before login.

        **Do not log**
        This is a highly sensitive credential, and gives access to an authenticated session.
        """
        return self.http_client.cookies.get("JSESSIONID")

    @property
    def pair(self) -> SessionPair:
        """The session pair being currently used. Useful for `resume`.

        Raises:
            MErrorMissingSessionIdentity: If the session has not logged in yet.
        """
        jsessionid = self.jsessionid
        if not jsessionid:
            raise MErrorMissingSessionIdentity
        return SessionPair(jsessionid=jsessionid, session_id=self.session_id)

    async def send(self, payload: str | list[str]) -> str:
        oci_xml = build_broadsoft_envelope(payload, self.session_id)
        soap_envelope = wrap_soap(oci_xml)

        response = await self.http_client.post(
            self.endpoint,
            content=soap_envelope.encode("utf-8"),
            headers={"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": ""},
        )
        response.raise_for_status()

        return unwrap_soap(response.text)

    async def close(self) -> None:
        """Close the httpx client, taking its cookie jar and sockets with it."""
        try:
            await self.http_client.aclose()
        except Exception:
            pass
