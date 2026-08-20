import logging
from itertools import batched
from typing import Self, overload

import attrs

from mercury_ocip_fast.authenticator import Authenticator
from mercury_ocip_fast.commands.base_command import OCIRequest, OCIResponse
from mercury_ocip_fast.requester import Requester
from mercury_ocip_fast.session.session import (
    ResumableSessionAtom,
    SessionPair,
    SOAPSessionSettings,
)

# The atom this client leases. Bound to the resumable (SOAP) shape, because
# ``resume`` and ``pair`` are the whole point of this client. A raw TCP socket
# cannot resume a login, so it does not fit here.
type SoapAtom = ResumableSessionAtom[SOAPSessionSettings]


@attrs.define(kw_only=True)
class SessionClient:
    """OCI-P client.

    A multi-tenant client. It holds no identity and keeps no session list.
    The client shares one requester across all sessions; each opened session is
    owned by its caller and may be passed around, closed, or exported and later
    resumed.

    Set up the client with ``create()`` or an ``async with`` block.

    Attributes:
        host: BroadWorks server hostname or address.
        port: BroadWorks server port. If ``None``, the endpoint or scheme
            supplies it.
        atom_type: Resumable session class to open, such as the SOAP atom.
        session_config: Transport settings for each session.
        tls: Whether to use TLS. If true, the client uses plain-text login; if
            false, it uses the encrypted login.
    """

    host: str
    port: int | None = None
    atom_type: type[SoapAtom]
    session_config: SOAPSessionSettings = attrs.field()
    tls: bool = True
    logger: logging.Logger = attrs.field(default=logging.getLogger(__name__))
    _requester: Requester = attrs.field(init=False)

    async def _async_setup(self) -> Self:
        """Set up the shared requester. Idempotent.

        Returns:
            The client.
        """
        if getattr(self, "_requester", None) is not None:
            return self

        self.logger.info(
            f"Initializing session client for {self.host}:{self.port} (tls={self.tls})"
        )
        self._requester = Requester()

        return self

    async def _login_factory(self, username: str, password: str) -> SoapAtom:
        """Open a session and log it in as the given user.

        Each caller brings its own credentials, so the authenticator is
        built per login rather than held on the client.
        """
        atom = await self.atom_type.open(
            self.host,
            self.port,
            settings=self.session_config,
            verify_ssl=self.tls,
        )

        authenticator = Authenticator(
            username=username, password=password, requester=self._requester
        )
        try:
            if (
                self.tls
            ):  # TLS protects the password, so a TLS link uses the plain-text login.
                await authenticator.generic_login(atom)
            else:
                await authenticator.encrypted_login(atom)
        except BaseException:
            await atom.close()
            raise

        return atom

    async def _resume_factory(self, pair: SessionPair) -> SoapAtom:
        """Open a session from a stored pair, without a fresh login."""
        return await self.atom_type.resume(
            self.host,
            pair,
            settings=self.session_config,
            verify_ssl=self.tls,
        )

    async def __aenter__(self) -> Self:
        """Do the async setup at the start of an ``async with`` block.

        Returns:
            The client, now ready for use.
        """
        return await self._async_setup()

    async def __aexit__(
        self, _exc_type: object, _exc_val: object, _exc_tb: object
    ) -> None:
        """Leave the ``async with`` block.

        The client owns no sessions, so it has nothing to close here. The
        caller must close each session it opened.
        """

    @classmethod
    async def create(cls, **kwargs) -> Self:
        """Make a client and do the async setup, without an ``async with``.

        Args:
            kwargs: The keyword arguments for the client, for example the
                host and the atom type.

        Returns:
            A client that is ready for use.
        """
        self = cls(**kwargs)
        return await self._async_setup()

    async def open(self, username: str, password: str) -> SoapAtom:
        """Open a fresh session, logged in as ``username``.

        The returned atom is the caller's handle. Pass it to ``command`` and
        close it with ``close`` when done.

        Args:
            username: The user name for the login.
            password: The password for the login. Treat this as a secret.

        Returns:
            A logged-in session, owned by the caller.

        Raises:
            MErrorLogin: If the server rejects the login.
        """
        return await self._login_factory(username, password)

    async def resume(self, pair: SessionPair) -> SoapAtom:
        """Open a session from a stored pair, resuming an earlier login.

        Use the atom's ``pair`` property to export the identity for later.

        Args:
            pair: The stored identity from an earlier session.

        Returns:
            A resumed session, owned by the caller.
        """
        return await self._resume_factory(pair)

    @overload
    async def command[R: OCIResponse](
        self, session: SoapAtom, request: OCIRequest[R]
    ) -> R: ...

    @overload
    async def command[R: OCIResponse](
        self, session: SoapAtom, request: list[OCIRequest[R]]
    ) -> list[R]: ...

    @overload
    async def command[R: OCIResponse](
        self, session: SoapAtom, request: OCIRequest, *, response_type: type[R]
    ) -> R: ...

    @overload
    async def command[R: OCIResponse](
        self, session: SoapAtom, request: list[OCIRequest], *, response_type: type[R]
    ) -> list[R]: ...

    async def command[R: OCIResponse](
        self,
        session: SoapAtom,
        request: OCIRequest[R] | list[OCIRequest[R]],
        *,
        response_type: type[R] | None = None,
    ) -> R | list[R]:
        """Send one command, or a batch, over the caller's session.

        The result is typed as the request's own response class, resolved
        from ``OCIRequest[R]``. Pass ``response_type`` only to override that
        with an explicit class.

        Args:
            session: The caller's session, from ``open`` or ``resume``.
            request: One OCI request, or a list of requests for a batch.
            response_type: The class to parse each response into. If None,
                each request's ``_response_cls`` is used.

        Returns:
            The parsed response, or a list of responses for a batch.
        """
        all_results: list[R] = []

        if isinstance(request, OCIRequest):
            return await self._requester.send(
                payload=request.to_xml(),
                response_type=response_type or request._response_cls,
                session=session,
            )

        for batch in batched(request, 15):
            result = await self._requester.send(
                payload=[b.to_xml() for b in batch],
                response_type=response_type or [b._response_cls for b in batch],
                session=session,
            )
            all_results.extend(result)

        return all_results

    async def close(self, session: SoapAtom) -> None:
        """Close one of the caller's sessions and let go of its transport.

        Args:
            session: The session to close.
        """
        await session.close()
