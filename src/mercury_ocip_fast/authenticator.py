import hashlib
import logging

import attrs

from mercury_ocip_fast.commands.commands import (
    AuthenticationRequest,
    AuthenticationResponse,
    LoginRequest14sp4,
    LoginRequest22V5,
    LoginResponse14sp4,
    LoginResponse22V5,
)
from mercury_ocip_fast.exceptions import MErrorLogin, MErrorResponse
from mercury_ocip_fast.requester import Requester
from mercury_ocip_fast.session.session import SessionAtom

logger = logging.getLogger(__name__)


@attrs.define(kw_only=True, slots=True)
class Authenticator:
    """Logs a session in via two options:

    `encrypted_login`: for encrypting the password for insecure networks.
    `generic_login`: standard plaintext auth for SSL/TLS connections."""

    username: str = attrs.field()
    password: str = attrs.field()
    requester: Requester = attrs.field()

    def _sign(self, nonce: str) -> str:
        authhash = hashlib.sha1(self.password.encode()).hexdigest().lower()
        return hashlib.md5(f"{nonce}:{authhash}".encode()).hexdigest().lower()

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

        try:
            auth_resp = await self.requester.send(
                payload=AuthenticationRequest(user_id=self.username).to_xml(),
                response_type=AuthenticationResponse,
                session=session,
            )

            signed = self._sign(auth_resp.nonce)

            return await self.requester.send(
                payload=LoginRequest14sp4(
                    user_id=self.username, signed_password=signed
                ).to_xml(),
                response_type=LoginResponse14sp4,
                session=session,
            )
        except MErrorResponse as e:
            logger.warning(
                "The encrypted login failed for user %s: %s", self.username, e.message
            )
            raise MErrorLogin(e.message) from e

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

        try:
            return await self.requester.send(
                payload=LoginRequest22V5(
                    user_id=self.username, password=self.password
                ).to_xml(),
                response_type=LoginResponse22V5,
                session=session,
            )
        except MErrorResponse as e:
            logger.warning(
                "The generic login failed for user %s: %s", self.username, e.message
            )
            raise MErrorLogin(e.message) from e
