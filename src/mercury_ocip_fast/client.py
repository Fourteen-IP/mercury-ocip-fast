import logging
from itertools import batched
from typing import Self, overload

import attrs

from mercury_ocip_fast.authenticator import Authenticator
from mercury_ocip_fast.commands.base_command import OCIRequest, OCIResponse
from mercury_ocip_fast.pool.session_pool import SessionPool, SessionPoolSettings
from mercury_ocip_fast.requester import Requester
from mercury_ocip_fast.session.session import (
    SessionAtom,
    SOAPSessionSettings,
    TCPSessionSettings,
)


@attrs.define(kw_only=True)
class Client[S: (TCPSessionSettings, SOAPSessionSettings)]:
    """The top level entry point for OCI-P commands.

    The client opens sessions, it logs them in as the specified user,
    and sends commands over them.

    The client needs an async setup step before use. Make the client
    with the ``create`` method, or use it in an ``async with`` block.

    Attributes:
        host: The host name or address of the BroadWorks server.
        port: The port of the server. If None, the endpoint or the
            scheme sets the port.
        username: The user name for the login.
        password: The password for the login. Treat this value as a
            secret.
        atom_type: The class of session to open, for example the SOAP
            atom or the TCP atom.
        session_config: The transport settings for each session, for
            example the timeouts.
        pool_config: The settings for the session pool, for example the
            maximum size.
        user_agent: The user agent name for the client.
        tls: If true, use a TLS link. A TLS link protects the password,
            so the client uses the plain-text login. If false, the
            client uses the encrypted login.
    """

    host: str
    port: int | None = None
    username: str
    password: str
    atom_type: type[SessionAtom[S]]
    session_config: S = attrs.field()
    pool_config: SessionPoolSettings = attrs.field()
    tls: bool = True
    logger: logging.Logger = attrs.field(default=logging.getLogger(__name__))
    _requester: Requester = attrs.field(init=False)
    _pool: SessionPool[SessionAtom[S]] = attrs.field(init=False)
    _authenticator: Authenticator = attrs.field(init=False)

    async def _async_setup(self) -> Self:
        """Setup the client and its dependencies.

        Returns:
            The client
        """
        if getattr(self, "_pool", None) is not None:
            return self

        self.logger.info(
            f"Initializing requester for {self.host}:{self.port} (tls={self.tls})"
        )
        self._requester = Requester()
        self._authenticator = Authenticator(
            username=self.username, password=self.password, requester=self._requester
        )
        self._pool = SessionPool(
            default_factory=self._default_factory,
            pool_settings=self.pool_config,
        )

        return self

    async def _default_factory(self) -> SessionAtom[S]:
        """Create a SessionAtom and log it in based on the client's TLS value."""
        atom = await self.atom_type.open(
            self.host,
            self.port,
            settings=self.session_config,
            verify_ssl=self.tls,
        )

        try:
            if (
                self.tls
            ):  # TLS protects the password, so a TLS link uses the plain-text login.
                await self._authenticator.generic_login(atom)
            else:
                await self._authenticator.encrypted_login(atom)
        except BaseException:
            await atom.close()
            raise

        return atom

    async def __aenter__(self) -> Self:
        """Do the async setup at the start of an ``async with`` block.

        Returns:
            The client, now ready for use.
        """
        return await self._async_setup()

    async def __aexit__(
        self, _exc_type: object, _exc_val: object, _exc_tb: object
    ) -> None:
        """Close the client at the end of an ``async with`` block.

        This method closes the client for each exit. It closes the
        client after an error, and it closes the client after a normal
        exit.
        """
        await self.close()

    @classmethod
    async def create(cls, **kwargs) -> Self:
        """Make a client and do the async setup.

        Use this method to get a client without an ``async with`` block.
        Keep the client, use it, and then close it with the ``close``
        method.

        Args:
            kwargs: The keyword arguments for the client, for example
                the host, the user name, and the password.

        Returns:
            A client that is ready for use.
        """
        self = cls(**kwargs)
        return await self._async_setup()

    @overload
    async def command[R: OCIResponse](self, request: OCIRequest[R]) -> R: ...

    @overload
    async def command[R: OCIResponse](
        self, request: list[OCIRequest[R]]
    ) -> list[R]: ...

    @overload
    async def command[R: OCIResponse](
        self, request: OCIRequest, *, response_type: type[R]
    ) -> R: ...

    @overload
    async def command[R: OCIResponse](
        self, request: list[OCIRequest], *, response_type: type[R] | list[R]
    ) -> list[R]: ...

    async def command[R: OCIResponse](
        self,
        request: OCIRequest[R] | list[OCIRequest[R]],
        *,
        response_type: type[R] | None = None,
    ) -> R | list[R]:
        """Send one command, or a batch, and return the parsed response(s).

        The result is typed as the request's own response class, resolved
        from ``OCIRequest[R]``. Pass ``response_type`` only to override that
        with an explicit class.

        Args:
            request: One OCI request, or a list of requests for a batch.
            response_type: The class to parse each response into. If None,
                each request's ``_response_cls`` is used.

        Returns:
            The parsed response, or a list of responses for a batch.
        """
        all_results: list[R] = []

        async with self._pool.session() as atom:
            if isinstance(request, OCIRequest):
                return await self._requester.send(
                    payload=request.to_xml(),
                    response_type=response_type or request._response_cls,
                    session=atom,
                )

            for batch in batched(request, 15):
                result = await self._requester.send(
                    payload=[b.to_xml() for b in batch],
                    response_type=response_type or [b._response_cls for b in batch],
                    session=atom,
                )
                all_results.extend(result)

            return all_results

    async def close(self) -> None:
        """Close the client and every session in the pool.

        This method closes the session pool. The pool closes each
        session and lets go of each transport. The method is safe to
        call more than once.
        """
        pool = getattr(self, "_pool", None)  # Survives half constructed client
        if pool is not None:
            await pool.close()
