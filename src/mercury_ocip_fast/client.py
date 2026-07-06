import asyncio
import hashlib
import logging
import sys
import uuid
from abc import ABC, abstractmethod
from typing import Awaitable, Callable, Literal, Optional, Union, cast, overload

import attrs

from mercury_ocip_fast.commands import commands
from mercury_ocip_fast.commands.base_command import (
    ErrorResponse,
    OCICommand,
    OCIRequest,
    SuccessResponse,
    TResponse,
)
from mercury_ocip_fast.commands.commands import (
    AuthenticationRequest,
    AuthenticationResponse,
    LoginRequest14sp4,
    LoginRequest22V5,
    LoginResponse14sp4,
    LoginResponse22V5,
)
from mercury_ocip_fast.exceptions import (
    MError,
    MErrorFailedAuthentication,
    MErrorResponse,
)
from mercury_ocip_fast.libs.types import CommandInput, CommandResult, RequestResult
from mercury_ocip_fast.pool import PoolConfig, PooledConnection, SOAPPoolConfig
from mercury_ocip_fast.requester import AsyncSOAPRequester, AsyncTCPRequester
from mercury_ocip_fast.soap_pool import SOAPSession
from mercury_ocip_fast.utils.parser import Parser


class FakeDispatchTable:
    # Dispatch table was removed for performance,
    # but Agent requires it in some spaces, this is for backwards compatibility

    def __init__(self, client: "Client") -> None:
        self._client = client

    def get(self, command_name: str, default: object = None) -> object:
        return getattr(commands, command_name, default)


class BaseClient(ABC):
    """Abstract base for all async BroadWorks OCI-P clients."""

    @overload
    async def command(
        self, command: OCIRequest[TResponse]
    ) -> Union[SuccessResponse, TResponse]: ...
    @overload
    async def command(self, command: list[CommandInput]) -> list[CommandResult]: ...

    @abstractmethod
    async def command(
        self, command: Union[CommandInput, list[CommandInput]]
    ) -> Union[CommandResult, list[CommandResult]]:
        """Run one or more OCI-P commands."""
        pass

    @abstractmethod
    async def shutdown(self, wait_timeout: float = 30.0) -> None:
        """Close everything down cleanly."""
        pass

    @property
    @abstractmethod
    def pool_stats(self) -> dict[str, int]:
        """How busy the underlying connection/session pool is."""
        pass

    def _receive_response(
        self, response: Union[RequestResult, list[str]]
    ) -> Union[CommandResult, list[CommandResult]]:
        if isinstance(response, MError):
            raise response

        if isinstance(response, list):
            results: list[CommandResult] = []
            for batch_xml in response:
                batch_results = self._parse_response(batch_xml)
                if isinstance(batch_results, list):
                    results.extend(batch_results)
                else:
                    results.append(batch_results)
            return results

        if isinstance(response, str):
            return self._parse_response(response)

        raise MError("Unexpected response type")

    def _parse_response(
        self, response: str
    ) -> Union[CommandResult, list[CommandResult]]:
        response_dict = Parser.to_dict_from_xml(response)
        command_data = response_dict.get("command")

        if command_data is None:
            return SuccessResponse()

        if isinstance(command_data, list):
            return [self._parse_single_command(cmd) for cmd in command_data]

        if isinstance(command_data, dict):
            return self._parse_single_command(command_data)

        return SuccessResponse()

    def _parse_single_command(self, command_data: dict) -> CommandResult:
        type_name: Union[str, None] = command_data.get("attributes", {}).get(
            "{http://www.w3.org/2001/XMLSchema-instance}type"
        )

        if not type_name or not isinstance(type_name, str):
            raise MError("Failed to parse response object")

        if ":" in type_name:
            type_name = type_name.split(":", 1)[1]

        if type_name == "ErrorResponse":
            return cast(ErrorResponse, ErrorResponse.from_dict(command_data))

        if type_name == "SuccessResponse":
            return SuccessResponse.from_dict(command_data)

        response_class = getattr(commands, type_name, None)

        if not response_class:
            raise MError(f"Failed To Find Raw Response Type: {type_name}")

        return response_class.from_dict(command_data)

    def _set_up_logging(self) -> logging.Logger:
        logger = logging.getLogger(__name__)
        logger.setLevel(logging.WARNING)
        if not logger.hasHandlers():
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setLevel(logging.WARNING)
            logger.addHandler(console_handler)
        return logger


@attrs.define(kw_only=True)
class Client(BaseClient):
    """Async client for the BroadWorks OCI-P API, over TCP or SOAP.

    Either way the client keeps a pool of logged-in connections (TCP) or
    sessions (SOAP) so requests can run side by side. Opening connections can take time
    so its advised to use ``client.warm()` to open connections before sending commands.

    SOAP will take longer to open sessions than TCP.

    Pass ``command()`` a list and it batches automatically, 15 per message, as
    the OCI-P spec suggests.

    When TLS is off the login hashes your password (``LoginRequest14sp4``) so it
    never gets sent in plaintext; with TLS on, the TCP socket is wrapped with SSL.
    When TLS is on it will use the more modern ``LoginRequest22V5``. This is the same for SOAP.

    SOAP has both options for HTTP/HTTPS dependent on the input host, since there is no socket to wrap:
        TLS ON - ``LoginRequest22V5``
        TLS OFF - ``LoginRequest14sp4`` (Hashed Password)

    Args:
        host: Hostname/IP for TCP, or the full SOAP endpoint URL for SOAP (without ?wsdl).
        username: Login username.
        password: Login password.
        conn_type: ``"TCP"`` (default) or ``"SOAP"``.
        port: Server port. Defaults to 2209; ignored for SOAP (the URL has it). Usually 2209 for secure, 2208 for non-secure.
        config: Pool config. Defaults to PoolConfig for TCP, SOAPPoolConfig for SOAP.
        user_agent: User-agent string used in logs.
        session_id: Session id; generated for you if you don't pass one.
        tls: Use TLS/HTTPS. On by default.

    Raises:
        ValueError: if conn_type isn't ``"TCP"`` or ``"SOAP"``.
        MError: if login fails.
    """

    host: str
    username: str
    password: str
    conn_type: Literal["TCP", "SOAP"] = "TCP"
    port: int = 2209
    config: Union[PoolConfig, SOAPPoolConfig] = attrs.Factory(
        lambda self: PoolConfig() if self.conn_type == "TCP" else SOAPPoolConfig(),
        takes_self=True,
    )
    user_agent: str = "Mercury OCIP Fast Client"
    session_id: str = attrs.Factory(lambda: str(uuid.uuid4()))
    tls: bool = True

    _authenticated: bool = attrs.field(default=False, init=False)
    _auth_lock: asyncio.Lock = attrs.field(init=False)
    _requester: Union[AsyncTCPRequester, AsyncSOAPRequester] = attrs.field(init=False)
    logger: logging.Logger = attrs.Factory(
        lambda self: self._set_up_logging(), takes_self=True
    )

    def __attrs_post_init__(self) -> None:
        if self.conn_type not in ("TCP", "SOAP"):
            raise ValueError(
                f"conn_type must be 'TCP' or 'SOAP', got {self.conn_type!r}"
            )
        self._auth_lock = asyncio.Lock()
        if self.conn_type == "TCP":
            self._requester = AsyncTCPRequester(
                host=self.host,
                port=self.port,
                config=cast(PoolConfig, self.config),
                tls=self.tls,
                session_id=self.session_id,
                logger=self.logger,
                auth_callback=self._create_tcp_auth_callback(),
            )
        else:
            self._requester = AsyncSOAPRequester(
                host=self.host,
                config=cast(SOAPPoolConfig, self.config),
                session_id=self.session_id,
                logger=self.logger,
                auth_callback=self._create_soap_auth_callback(),
            )

    def __getattr__(self, name: str) -> object:
        if name == "_dispatch_table":
            return FakeDispatchTable(self)
        raise AttributeError(f"'{type(self).__name__}' has no attribute '{name}'")

    async def __aenter__(self) -> "Client":
        return self

    async def __aexit__(
        self, _exc_type: object, _exc_val: object, _exc_tb: object
    ) -> None:
        await self.shutdown()

    def _create_tcp_auth_callback(
        self,
    ) -> Callable[[PooledConnection], Awaitable[None]]:
        async def _on_new_connection(conn: PooledConnection) -> None:
            await self.authenticate(conn=conn)

        return _on_new_connection

    def _create_soap_auth_callback(
        self,
    ) -> Callable[[SOAPSession], Awaitable[None]]:
        async def _on_new_session(session: SOAPSession) -> None:
            await self.authenticate(session=session)

        return _on_new_session

    @overload
    async def command(
        self, command: OCIRequest[TResponse]
    ) -> Union[SuccessResponse, TResponse]: ...
    @overload
    async def command(self, command: list[CommandInput]) -> list[CommandResult]: ...

    async def command(
        self, command: Union[CommandInput, list[CommandInput]]
    ) -> Union[CommandResult, list[CommandResult]]:
        """Run one command, or a list of them.

        Logs in on the first call if it hasn't already (Auth timeout).

        Give it a single command and you get its response back — a single
        failed command raises ``MErrorResponse`` (with the decoded
        ``ErrorResponse`` on ``.context``) rather than returning the error, so
        the happy path needs no isinstance checks.

        Give it a list and it batches them into concurrent requests (per the
        OCI-P spec) and returns a list of results in the order they were sent.
        Batch results keep failures as ``ErrorResponse`` values in the list so
        that failed commands dont ruin the whole list.

        Args:
            command: One command, or a list of them.

        Returns:
            A single response for a single command, or a list for a list.

        Raises:
            MErrorResponse: if a single command comes back as an ``ErrorResponse``.
            MError: if a response can't be parsed or the transport fails.
        """
        # TCP authenticates the client up front; SOAP sessions each authenticate
        # themselves the moment the pool creates them (via the auth callback).
        if self.conn_type == "TCP" and not self._authenticated:
            async with self._auth_lock:
                if not self._authenticated:
                    await self.authenticate()

        if isinstance(command, list):
            self.logger.debug(
                f"Dispatching {len(command)} {self.conn_type} commands: "
                f"{[type(cmd).__name__ for cmd in command]}"
            )
            xml_commands = [cmd.to_xml() for cmd in cast(list[OCICommand], command)]
            responses = await self._requester.send_bulk_request(xml_commands)
            return self._receive_response(responses)

        self.logger.debug(
            f"Dispatching {self.conn_type} command: {type(command).__name__}"
        )
        response = await self._requester.send_request(
            cast(OCICommand, command).to_xml()
        )
        result = self._receive_response(response)
        if isinstance(result, ErrorResponse):
            raise MErrorResponse(
                message=f"{type(command).__name__} failed: {result.summary}",
                context=result,
            )
        return result

    async def authenticate(
        self,
        conn: Optional[PooledConnection] = None,
        session: Optional[SOAPSession] = None,
    ) -> Optional[Union[LoginResponse22V5, LoginResponse14sp4]]:
        """Log in to the BroadWorks server.

        With TLS it's a single login; without it, the two-step hashed-password
        dance instead. TCP and SOAP follow the same OCI-P sequence either way.

        Pass a ``conn`` to log in that specific TCP connection, or a ``session``
        to log in that specific SOAP session for fine-grained connection management.

        Returns:
            The login response, or None if we were already logged in.

        Raises:
            MError: If authentication fails.
        """
        if conn is None and session is None and self._authenticated:
            return None

        if self.conn_type == "TCP":

            async def _send(xml: str) -> str:
                return await cast(AsyncTCPRequester, self._requester).send_request(
                    xml, conn=conn
                )

            target = (
                f"connection {conn.session_id}"
                if conn is not None
                else f"session {self.session_id}"
            )
        else:

            async def _send(xml: str) -> str:
                return await cast(AsyncSOAPRequester, self._requester).send_request(
                    xml, session=session
                )

            target = (
                f"session {session.session_id}"
                if session is not None
                else f"session {self.session_id}"
            )

        self.logger.debug(
            f"Authenticating {self.username!r} via {'TLS' if self.tls else 'non-TLS'} "
            f"({target})"
        )
        login_response = await self._login(_send)

        self.logger.info(f"{self.username} authenticated ({target})")
        self._authenticated = True
        return login_response

    async def _login(
        self, send: Callable[[str], Awaitable[str]]
    ) -> Union[LoginResponse22V5, LoginResponse14sp4]:
        """Walk through the OCI-P login steps, sending each one via ``send``.

        Raises:
            MError: if any step of the login fails.
        """
        if self.tls:
            login_response = self._receive_response(
                await send(
                    LoginRequest22V5(
                        user_id=self.username, password=self.password
                    ).to_xml()
                )
            )
            if isinstance(login_response, ErrorResponse):
                raise MErrorFailedAuthentication(
                    f"Failed to authenticate: {login_response.summary}"
                )
            return cast(LoginResponse22V5, login_response)

        auth_response = self._receive_response(
            await send(AuthenticationRequest(user_id=self.username).to_xml())
        )
        if isinstance(auth_response, ErrorResponse):
            raise MErrorFailedAuthentication(
                f"Auth request failed: {auth_response.summary}"
            )
        if not isinstance(auth_response, AuthenticationResponse):
            raise MError("Unexpected response from AuthenticationRequest")

        authhash = hashlib.sha1(self.password.encode()).hexdigest().lower()
        signed_password = (
            hashlib.md5(f"{auth_response.nonce}:{authhash}".encode())
            .hexdigest()
            .lower()
        )
        login_response = self._receive_response(
            await send(
                LoginRequest14sp4(
                    user_id=self.username, signed_password=signed_password
                ).to_xml()
            )
        )
        if isinstance(login_response, ErrorResponse):
            raise MErrorFailedAuthentication(
                f"Failed to authenticate: {login_response.summary}"
            )
        return cast(LoginResponse14sp4, login_response)

    async def warm(self, count: int | None = None) -> int:
        """Open a defined amount of connections and authenticate them.

        On TCP that's ``count`` connections (defaults to the pool's
        max_connections); on SOAP it's ``count`` sessions (defaults to
        pool_size), each of which fetches the WSDL and logs in up front.

        Args:
            count: How many connections/sessions to open.

        Returns:
            How many actually authenticated and came up.
        """
        return await self._requester.warm(count)

    def export_soap_session(self) -> tuple[str, str] | None:
        """The (JSESSIONID, OCI-P sessionId) identity of a logged-in SOAP session.

        Returns the pair from the first pooled session, or None if no session
        has logged in yet. The two values only work together as BroadWorks pairs
        the cookie with the in-body session id at login and rejects requests
        carrying a mismatched pair, so never store or hand out one without the
        other.

        The exported identity outlives ``shutdown()``: closing this client only
        drops the local httpx clients, while BroadWorks keeps the session alive
        server-side until its idle timeout or an explicit ``LogoutRequest``.
        Resume it elsewhere with ``AsyncSOAPRequester.detached_session()``.

        Raises:
            ValueError: on a TCP client as TCP sessions live and die with their
                socket and can't be exported.
        """
        if self.conn_type != "SOAP":
            raise ValueError("export_soap_session() only works on SOAP clients")
        pool = cast(AsyncSOAPRequester, self._requester)._pool
        if not pool or not pool._all_sessions:
            return None
        session = pool._all_sessions[0]
        jsessionid = session.jsessionid
        if jsessionid is None:
            return None
        return jsessionid, session.session_id

    async def shutdown(self, wait_timeout: float = 30.0) -> None:
        """Close the client down and let go of everything it's holding.

        Args:
            wait_timeout: How long to wait for in-flight work to finish first.
        """
        self.logger.info(f"Shutting down {self.conn_type} client...")
        self._authenticated = False
        await self._requester.close(wait_timeout=wait_timeout)
        self.logger.info(f"{self.conn_type} client shutdown complete")

    @property
    def pool_stats(self) -> dict[str, int]:
        """How busy the pool is right now, for monitoring.

        TCP reports on connections, SOAP on sessions.
        """
        if self.conn_type == "TCP" and isinstance(self._requester, AsyncTCPRequester):
            if self._requester._pool:
                return self._requester._pool.stats
            cfg = cast(PoolConfig, self.config)
            return {
                "total_connections": 0,
                "available": 0,
                "in_use": 0,
                "waiting": 0,
                "max_connections": cfg.max_connections,
                "max_concurrent": cfg.max_concurrent_requests,
            }

        if isinstance(self._requester, AsyncSOAPRequester) and self._requester._pool:
            return self._requester._pool.stats

        cfg_soap = cast(SOAPPoolConfig, self.config)
        return {
            "total_sessions": 0,
            "available": 0,
            "in_use": 0,
            "pool_size": cfg_soap.pool_size,
        }

    @property
    def session_ids(self) -> list[str]:
        """The session ids of every live, logged-in connection or session.

        One id per pooled SOAP session, or one per pooled TCP connection. You
        only see ids for what's actually been opened, so call :meth:`warm` first
        if you want the whole pool listed.

        Treat these as secrets - anyone can access authenticated sessions and send commands with them.
        """
        if isinstance(self._requester, AsyncSOAPRequester):
            return self._requester.session_ids
        if isinstance(self._requester, AsyncTCPRequester) and self._requester._pool:
            return [c.session_id for c in self._requester._pool._all_connections]
        return []
