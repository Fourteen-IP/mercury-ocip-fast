import attrs
import sys
import logging
import hashlib
import uuid
from typing import Union, overload, Optional

from mercury_ocip_fast.commands.commands import (
    LoginRequest22V5,
    AuthenticationRequest,
    LoginRequest14sp4,
    AuthenticationResponse,
)
from mercury_ocip_fast.commands import commands
from mercury_ocip_fast.commands.base_command import ErrorResponse, SuccessResponse
from mercury_ocip_fast.requester import AsyncTCPRequester
from mercury_ocip_fast.pool import PoolConfig, PooledConnection
from mercury_ocip_fast.exceptions import MError
from mercury_ocip_fast.utils.parser import Parser
from mercury_ocip_fast.libs.types import (
    RequestResult,
    CommandInput,
    CommandResult,
)


class FakeDispatchTable:
    # Dispatch table was removed for performance,
    # but Agent requires it in some spaces, this is for backwards compatability

    def __init__(self, client):
        self._client = client

    def get(self, command_name, default=None):
        return getattr(commands, command_name, default)


@attrs.define(kw_only=True)
class Client:
    """Async client for BroadWorks OCI-P API.

    Args:
        host: Hostname or IP address of the BroadWorks server.
        port: Server port. Defaults to 2209 (TLS).
        username: Authentication username.
        password: Authentication password.
        config: Connection pool configuration.
        user_agent: User agent string for logging.
        logger: Custom logger instance. Creates default if not provided.
        session_id: Session identifier. Auto-generated if not provided.
        tls: Use TLS encryption. Defaults to True.

    Raises:
        MError: If authentication fails.
    """

    host: str
    port: int = 2209
    username: str
    password: str
    config: PoolConfig = attrs.Factory(PoolConfig)
    user_agent: str = "Broadworks SDK"
    session_id: str = attrs.Factory(lambda: str(uuid.uuid4()))
    tls: bool = True

    _authenticated: bool = attrs.field(default=False, init=False)
    _requester: AsyncTCPRequester = attrs.field(init=False)
    logger: logging.Logger = attrs.Factory(
        lambda self: self._set_up_logging(), takes_self=True
    )

    def __attrs_post_init__(self):
        self._requester = AsyncTCPRequester(
            host=self.host,
            port=self.port,
            config=self.config,
            tls=self.tls,
            session_id=self.session_id,
            logger=self.logger,
            auth_callback=self._create_auth_callback(),
        )

    def __getattr__(self, name):
        if name == "_dispatch_table":
            return FakeDispatchTable(self)
        raise AttributeError(f"'{type(self).__name__}' has no attribute '{name}'")

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc_val, _exc_tb):
        await self._disconnect()

    def _create_auth_callback(self):
        async def _authenticate(conn: PooledConnection) -> None:
            await self.authenticate(conn)

        return _authenticate

    @overload
    async def command(self, command: CommandInput) -> CommandResult: ...

    @overload
    async def command(self, command: list[CommandInput]) -> list[CommandResult]: ...

    async def command(
        self, command: Union[CommandInput, list[CommandInput]]
    ) -> Union[CommandResult, list[CommandResult]]:
        """Execute one or more OCI-P commands.

        Authenticates automatically if needed. Accepts either a single command
        or a list of commands for bulk execution.

        Args:
            command: A single command instance or list of command instances.

        Returns:
            Single CommandResult for single input, list of CommandResult for list input.

        Raises:
            MError: If a command fails or response cannot be parsed.
        """
        if not self._authenticated:
            await self.authenticate()

        if isinstance(command, list):
            xml_commands = [cmd.to_xml() for cmd in command]
            responses = await self._requester.send_bulk_request(xml_commands)
            return self._receive_response(responses)
        else:
            response = await self._requester.send_request(command.to_xml())
            return self._receive_response(response)

    async def warm(self, connection_amount: int | None = None) -> int:
        """Pre-warm the connection pool for faster bulk requests.

        Args:
            connection_amount: Number of connections to create. Defaults to pool max.

        Returns:
            Number of connections created.
        """
        return await self._requester.warm(connection_amount)

    async def authenticate(
        self, conn: Optional[PooledConnection] = None
    ) -> CommandResult:
        """Authenticate with the BroadWorks server.

        Uses TLS direct login or two-stage hashed password authentication
        depending on the tls setting. Called automatically by command() if needed.

        Returns:
            Login response from server, or None if already authenticated.

        Raises:
            MError: If authentication fails.
        """
        if conn is None and self._authenticated:
            return None

        if self.tls:
            login_request = LoginRequest22V5(
                user_id=self.username, password=self.password
            )

            login_response = self._receive_response(
                await self._requester.send_request(login_request.to_xml(), conn=conn)
            )

            if isinstance(login_response, ErrorResponse):
                raise MError(f"Failed to authenticate: {login_response.summary}")

            self.logger.info(f"{self.username} Authenticated with server")
            self._authenticated = True
            return login_response
        else:
            # Non-TLS requires two-stage authentication with password hashing
            auth_request = AuthenticationRequest(user_id=self.username)

            auth_response = self._receive_response(
                await self._requester.send_request(auth_request.to_xml(), conn=conn)
            )

            if isinstance(auth_response, ErrorResponse):
                raise MError(f"Auth request failed: {auth_response.summary}")

            if not isinstance(auth_response, AuthenticationResponse):
                raise MError("Unexpected response type from AuthenticationRequest")

            authhash: str = hashlib.sha1(self.password.encode()).hexdigest().lower()
            signed_password: str = (
                hashlib.md5(f"{auth_response.nonce}:{authhash}".encode())
                .hexdigest()
                .lower()
            )

            # Complete login with signed password
            login_request = LoginRequest14sp4(
                user_id=self.username, signed_password=signed_password
            )

            login_response = self._receive_response(
                await self._requester.send_request(login_request.to_xml(), conn=conn)
            )

            if isinstance(login_response, ErrorResponse):
                raise MError(f"Failed to authenticate: {login_response.summary}")

            self.logger.info("Authenticated with server")
            self._authenticated = True
            return login_response

    def _receive_response(
        self, response: Union[RequestResult, list[str]]
    ) -> Union[CommandResult, list[CommandResult]]:
        """Parse requester response into command result(s).

        Handles both single responses and batch responses. Batch responses
        (from send_bulk_request) may contain multiple commands per XML document,
        which are flattened into a single list.

        Args:
            response: Single response string or list of batch response strings.

        Returns:
            Single CommandResult for single input, flattened list for batch input.

        Raises:
            MError: If response is an error or cannot be parsed.
        """
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
        """Parse XML response string into command result object(s).

        Handles responses with single or multiple command elements.

        Args:
            response: Raw XML response string from the server.

        Returns:
            Single CommandResult or list if response contains multiple commands.

        Raises:
            MError: If response cannot be parsed or command type is unknown.
        """
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
        """Parse a single command dict into a CommandResult.

        Args:
            command_data: Parsed command dictionary from XML.

        Returns:
            Parsed command result object.

        Raises:
            MError: If command type cannot be determined or is unknown.
        """
        type_name: Union[str, None] = command_data.get("attributes", {}).get(
            "{http://www.w3.org/2001/XMLSchema-instance}type"
        )

        if not type_name or not isinstance(type_name, str):
            raise MError("Failed to parse response object")

        if ":" in type_name:
            type_name = type_name.split(":", 1)[1]

        if type_name == "ErrorResponse":
            return ErrorResponse.from_dict(command_data)
        elif type_name == "SuccessResponse":
            return SuccessResponse.from_dict(command_data)

        response_class = getattr(commands, type_name, None)

        if not response_class:
            raise MError(f"Failed To Find Raw Response Type: {type_name}")

        return response_class.from_dict(command_data)

    async def _disconnect(self, wait_timeout: float = 10.0) -> None:
        """Disconnect from the server and close the connection pool.

        Args:
            wait_timeout: Maximum seconds to wait for in-flight operations to complete.
        """
        self._authenticated = False
        self.session_id = ""
        await self._requester.close(wait_timeout=wait_timeout)

    async def shutdown(self, wait_timeout: float = 30.0) -> None:
        """Gracefully shutdown the client, waiting for all operations to complete.

        Args:
            wait_timeout: Maximum seconds to wait for in-flight operations to complete.
        """
        self.logger.info("Initiating graceful shutdown...")
        await self._disconnect(wait_timeout=wait_timeout)
        self.logger.info("Client shutdown complete")

    @property
    def pool_stats(self) -> dict[str, int]:
        """Get current connection pool statistics for monitoring.

        Returns:
            Dictionary containing pool metrics:
            - total_connections: Total number of connections created
            - available: Number of connections available in the pool
            - in_use: Number of connections currently in use
            - waiting: Number of tasks waiting for a connection
            - max_connections: Maximum allowed connections
            - max_concurrent: Maximum concurrent requests allowed

        Usage:
            stats = client.pool_stats
            print(f"Pool usage: {stats['in_use']}/{stats['max_connections']}")
        """
        if self._requester and self._requester._pool:
            return self._requester._pool.stats
        return {
            "total_connections": 0,
            "available": 0,
            "in_use": 0,
            "waiting": 0,
            "max_connections": self.config.max_connections,
            "max_concurrent": self.config.max_concurrent_requests,
        }

    def _set_up_logging(self) -> logging.Logger:
        """Create default logger with WARNING level console output."""
        logger = logging.getLogger(__name__)
        logger.setLevel(logging.WARNING)

        # Only add handler if none exist to prevent handler accumulation
        if not logger.hasHandlers():
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setLevel(logging.WARNING)
            logger.addHandler(console_handler)

        return logger
