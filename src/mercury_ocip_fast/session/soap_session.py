from __future__ import annotations

import logging
import time
import uuid

import attrs
import httpx

from mercury_ocip_fast.exceptions import (
    MErrorHttpDropped,
    MErrorHttpInitialisation,
    MErrorHttpStatus,
    MErrorHttpTimeout,
    MErrorMissingSessionIdentity,
)
from mercury_ocip_fast.session.session import (
    SessionPair,
    SOAPSessionSettings,
)
from mercury_ocip_fast.utils.endpoints import override_url_port
from mercury_ocip_fast.utils.envelopes import (
    build_broadsoft_envelope,
    unwrap_soap,
    wrap_soap,
)

logger = logging.getLogger(__name__)


@attrs.define(kw_only=True, slots=True)
class SOAPSessionAtom:
    """A SOAP session for BroadWorks.

    The session owns its transport and its identity. The transport is an
    httpx client. The client has its own cookie jar. The cookie jar holds
    the JSESSIONID cookie after a login.

    The session also has an OCI-P ``session_id``. This id goes in the body
    of each message. The class makes a new UUID for the id by default.

    Attributes:
        endpoint: The URL of the SOAP service.
        http_client: The httpx client. It holds this session's cookie jar.
        settings: The timeouts for the httpx client.
        session_id: The OCI-P session id for each message body.
    """

    endpoint: str
    http_client: httpx.AsyncClient
    settings: SOAPSessionSettings = attrs.field(default=SOAPSessionSettings())
    session_id: str = attrs.field(factory=lambda: str(uuid.uuid4()))
    created_at: float = attrs.field(factory=time.monotonic)
    last_used: float = attrs.field(factory=time.monotonic)

    @classmethod
    async def open(
        cls,
        endpoint: str,
        port: int | None = None,
        *,
        settings: SOAPSessionSettings,
        verify_ssl: bool = True,
    ) -> SOAPSessionAtom:
        """Make a new SOAP session. The session is not logged in.

        The new session has its own httpx client. The client has no
        cookies. A login must run before the session can send commands.

        Args:
            endpoint: The full URL of the SOAP service. The URL can hold the
                port, for example ``https://host:8443/webservice``.
            port: The port of the service. This argument has priority over a
                port in the URL. If None, the URL, or the scheme, sets the
                port.
            settings: The timeouts for the httpx client.
            verify_ssl: If true, verify the TLS certificate of the server.

        Returns:
            A new SOAP session.

        Raises:
            MErrorHttpInitialisation: If the URL has no host.
        """
        try:
            url = override_url_port(endpoint, port)
        except ValueError as e:
            raise MErrorHttpInitialisation(str(e)) from e

        timeout = httpx.Timeout(
            connect=settings.connect_timeout,
            read=settings.read_timeout,
            write=settings.write_timeout,
        )

        logger.debug(
            "Open a SOAP session to %s, connect timeout %ss, read timeout %ss",
            url,
            settings.connect_timeout,
            settings.read_timeout,
        )

        return cls(
            endpoint=url,
            http_client=httpx.AsyncClient(
                verify=verify_ssl,
                timeout=timeout,
            ),
            settings=settings,
        )

    @classmethod
    async def resume(
        cls,
        endpoint: str,
        pair: SessionPair,
        *,
        settings: SOAPSessionSettings,
        verify_ssl: bool = True,
    ) -> SOAPSessionAtom:
        """Make a SOAP session from a stored session pair.

        Use this method to continue a session after a restart. The new
        session takes the JSESSIONID cookie and the OCI-P session id from
        the pair. The server then accepts the session as logged in.

        Args:
            endpoint: The URL of the SOAP service.
            pair: The stored identity. It holds the cookie and the id.
            settings: The timeouts for the httpx client.
            verify_ssl: If true, verify the TLS certificate of the server.

        Returns:
            A SOAP session that uses the given pair.
        """
        session = await cls.open(endpoint, settings=settings, verify_ssl=verify_ssl)
        session.http_client.cookies.set("JSESSIONID", pair.jsessionid)
        session.session_id = pair.session_id

        logger.debug("Resume a SOAP session to %s from a stored pair", endpoint)

        return session

    @property
    def jsessionid(self) -> str | None:
        """The JSESSIONID cookie of this session.

        The value is None before the login.

        Do not write this value to a log. It is a secret. A person who has
        this value can send commands as the user.
        """
        return self.http_client.cookies.get("JSESSIONID")

    @property
    def pair(self) -> SessionPair:
        """The session pair of this session. Store it to resume later.

        Raises:
            MErrorMissingSessionIdentity: If the session has no login yet.
        """
        jsessionid = self.jsessionid
        if not jsessionid:
            raise MErrorMissingSessionIdentity()
        return SessionPair(jsessionid=jsessionid, session_id=self.session_id)

    async def send(self, payload: str | list[str]) -> str:
        """Send one envelope to the server and return one reply.

        This method wraps the payload in the BroadsoftDocument and then in
        the SOAP envelope. It posts the envelope with the httpx client. The
        JSESSIONID cookie goes with the request. The method then gets the
        OCI reply from the response.

        Do not write the payload or the reply to a log. They can hold
        secrets, for example a password in a login command.

        Args:
            payload: One OCI command, or a list of OCI commands.

        Returns:
            The OCI reply as a string.

        Raises:
            MErrorHttpStatus: If the server returns a non-2xx HTTP status.
            MErrorHttpTimeout: If the request does not complete in time.
            MErrorHttpInitialisation: If the client cannot connect.
            MErrorHttpDropped: If the connection stops during the request.
        """
        oci_xml = build_broadsoft_envelope(payload, self.session_id)
        soap_envelope = wrap_soap(oci_xml)

        logger.debug("Send %d bytes to %s", len(soap_envelope), self.endpoint)

        try:
            response = await self.http_client.post(
                self.endpoint,
                content=soap_envelope.encode("utf-8"),
                headers={"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": ""},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            logger.warning(
                "The server %s returned HTTP %d", self.endpoint, e.response.status_code
            )
            raise MErrorHttpStatus(
                f"BroadWorks returned HTTP {e.response.status_code}",
                status=e.response.status_code,
            ) from e
        except httpx.TimeoutException as e:
            logger.warning("The request to %s timed out: %s", self.endpoint, e)
            raise MErrorHttpTimeout(str(e)) from e
        except httpx.ConnectError as e:
            logger.warning("Cannot connect to %s: %s", self.endpoint, e)
            raise MErrorHttpInitialisation(str(e)) from e
        except httpx.TransportError as e:  # a read, write, or protocol error
            logger.warning(
                "The connection to %s dropped mid-request: %s", self.endpoint, e
            )
            raise MErrorHttpDropped(str(e)) from e

        logger.debug(
            "Receive %d bytes from %s, HTTP %d",
            len(response.text),
            self.endpoint,
            response.status_code,
        )

        return unwrap_soap(response.text)

    async def close(self) -> None:
        """Close the httpx client.

        This closes the cookie jar and the open sockets. The method is safe
        to call more than once.
        """
        logger.debug("Close the SOAP session to %s", self.endpoint)
        try:
            await self.http_client.aclose()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "The connection to %s raised an exception while closing: %s",
                self.endpoint,
                e,
            )

    def is_alive(self) -> bool:
        """Tell if the session is still connected."""
        return not self.http_client.is_closed

    def is_stale(self) -> bool:
        """Tell if the session is past its time to live."""
        return (time.monotonic() - self.created_at) > self.settings.max_ttl_seconds
