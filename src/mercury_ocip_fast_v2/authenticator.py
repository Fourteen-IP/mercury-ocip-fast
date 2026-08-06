import hashlib
import logging
import xml.etree.ElementTree as ET

import attrs

from mercury_ocip_fast_v2.commands.commands import (
    AuthenticationRequest,
    AuthenticationResponse,
    LoginRequest14sp4,
    LoginRequest22V5,
    LoginResponse14sp4,
    LoginResponse22V5,
)
from mercury_ocip_fast_v2.exceptions import MErrorLogin, MErrorMalformedResponse
from mercury_ocip_fast_v2.session.session import SessionAtom
from mercury_ocip_fast_v2.utils.parser import Parser

logger = logging.getLogger(__name__)


@attrs.define(kw_only=True, slots=True)
class Authenticator:
    """Logs a session in via two options:

    `encrypted_login`: for encrypting the password for insecure networks.
    `generic_login`: standard plaintext auth for SSL/TLS connections."""

    username: str = attrs.field()
    password: str = attrs.field()

    def _is_error_response(self, payload: str) -> bool:
        """Tell if a response payload is an error.

        The method reads the ``xsi:type`` of the command in the payload.
        The method gives ``True`` when the type is ``ErrorResponse``.
        The method also gives ``True`` when the type is absent, because
        a response with no type is not safe to trust.

        Args:
            payload: The XML response from the server.

        Returns:
            ``True`` if the payload is an error. If not, ``False``.
        """

        response_dict = Parser.to_dict_from_xml(payload)
        command_data = response_dict.get("command")

        if not command_data:
            logger.debug("The response has no command element. Treat it as a success.")
            return False  # SuccessResponse contains nothing

        type_name: str | None = command_data.get("attributes", {}).get(
            "{http://www.w3.org/2001/XMLSchema-instance}type"
        )

        if not type_name:
            logger.warning(
                "The response command has no xsi:type. Treat it as an error."
            )
            return True  # Fallback, push down the error branch anyway

        logger.debug("The response command has type %s.", type_name)
        return type_name == "ErrorResponse"

    async def encrypted_login(self, session: SessionAtom) -> LoginResponse14sp4:
        """Log the session in with an encrypted password.

        The method uses the legacy OCI-P flow. First it sends an
        ``AuthenticationRequest`` to get a nonce. Then it signs the
        password with the nonce and sends a ``LoginRequest14sp4``. Use
        this flow on insecure networks, because the password does not go
        across the network as plain text.

        Args:
            session: The session that sends the requests to the server.

        Returns:
            The ``LoginResponse14sp4`` from the server.

        Raises:
            MErrorLogin: If the server rejects the authentication or the
                login.
        """

        logger.debug("Start the encrypted login for user %s.", self.username)

        auth_request = AuthenticationRequest(user_id=self.username).to_xml()

        auth_response = await session.send(auth_request)

        if self._is_error_response(auth_response):
            logger.warning(
                "The server rejected the authentication for user %s.", self.username
            )
            raise MErrorLogin

        nonce = AuthenticationResponse.from_xml(auth_response).nonce
        logger.debug("Got a nonce for user %s. Sign the password.", self.username)
        authhash = hashlib.sha1(self.password.encode()).hexdigest().lower()

        signed_password = (
            hashlib.md5(f"{nonce}:{authhash}".encode()).hexdigest().lower()
        )

        login_request = LoginRequest14sp4(
            user_id=self.username, signed_password=signed_password
        ).to_xml()

        login_response = await session.send(login_request)

        if self._is_error_response(login_response):
            logger.warning("The server rejected the login for user %s.", self.username)
            raise MErrorLogin

        logger.debug("The encrypted login for user %s is complete.", self.username)
        return LoginResponse14sp4.from_xml(login_response)

    async def generic_login(self, session: SessionAtom) -> LoginResponse22V5:
        """Log the session in with a plain-text password.

        The method sends one ``LoginRequest22V5`` with the password as
        plain text. Use this flow only on an SSL or TLS connection,
        because the connection protects the password.

        Args:
            session: The session that sends the request to the server.

        Returns:
            The ``LoginResponse22V5`` from the server.

        Raises:
            MErrorLogin: If the server rejects the login.
        """

        logger.debug("Start the generic login for user %s.", self.username)

        login_request = LoginRequest22V5(
            user_id=self.username, password=self.password
        ).to_xml()

        login_response = await session.send(login_request)

        if self._is_error_response(login_response):
            logger.warning("The server rejected the login for user %s.", self.username)
            raise MErrorLogin

        logger.debug("The generic login for user %s is complete.", self.username)
        return LoginResponse22V5.from_xml(login_response)
