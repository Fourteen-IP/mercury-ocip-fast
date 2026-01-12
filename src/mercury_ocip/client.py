import attr
import sys
import logging
import hashlib
import uuid
from typing import Awaitable, Dict, Type, Union
import inspect
from abc import ABC, abstractmethod
import importlib

from mercury_ocip.commands.commands import (
    LoginRequest22V5,
    AuthenticationRequest,
    LoginRequest14sp4,
)
from mercury_ocip import commands
from mercury_ocip.commands.base_command import OCICommand
from mercury_ocip.commands.base_command import OCIType
from mercury_ocip.commands.base_command import ErrorResponse
from mercury_ocip.commands.base_command import SuccessResponse
from mercury_ocip.requester import (
    AsyncTCPRequester,
)
from mercury_ocip.pool import PoolConfig
from mercury_ocip.exceptions import MError
from mercury_ocip.utils.parser import Parser, AsyncParser
from mercury_ocip.libs.types import (
    RequestResult,
    XMLDictResult,
    CommandInput,
    CommandResult,
)


@attr.s(slots=True, kw_only=True)
class Client:
    """


    Args:
        host (str): URL or IP address of server. Depends on connection type. If SOAP DO NOT include '?wsdl' in the end of the URL.
        username (str): The username of the user
        password (str): The password of the user
        conn_type (str): Either 'TCP' or 'SOAP'. TCP is the default.

        port (int): The port of the server. Default is 2209. Only used in TCP mode.
        secure (bool): Whether the connection is secure. Default is True. Only used in TCP mode. Password is hashed if not secure.

        timeout (int): The timeout of the client. Default is 30 seconds.
        user_agent (str): The user agent of the client, used for logging. Default is 'Thor\'s Hammer'.
        logger (logging.Logger): The logger of the client. Default is None.

    Attributes:
        authenticated (bool): Whether the client is authenticated
        session_id (str): The session id of the client
        _dispatch_table (dict): The dispatch table of the client

    Raises:
        Exception: If the client fails to authenticate
    """

    host: str = attr.ib()
    port: int = attr.ib(default=2209)
    username: str = attr.ib()
    password: str = attr.ib()
    config: PoolConfig = attr.ib(default=PoolConfig())
    user_agent: str = attr.ib(default="Broadworks SDK")
    logger: logging.Logger = attr.ib(default=None)
    session_id: str = attr.ib(default=str(uuid.uuid4()))
    tls: bool = attr.ib(default=True)

    _authenticated: bool = attr.ib(default=False)
    _requester: AsyncTCPRequester = attr.ib(default=None)

    def __attrs_post_init__(self):
        self.logger = self.logger or self._set_up_logging()
        self._requester = AsyncTCPRequester(
            host=self.host,
            port=self.port,
            config=self.config,
            tls=self.tls,
            session_id=self.session_id,
            logger=self.logger,
        )

    def command(self, command: CommandInput) -> CommandResult:
        """
        Executes all requests to the server.
        If the client is not authenticated, it will authenticate first.

        Args:
            command (OCIRequest): The command class to execute

        Returns:
            OCIResponse: The response from the server
        """
        pass

    def commands(self, commands: list[CommandInput]) -> list[CommandResult]:
        pass

    def authenticate(
        self,
    ) -> CommandResult:
        """
        Authenticates client with username and password in client.

        Note: Directly send request to requester to avoid double authentication

        Returns:
            OCIResponse: The response from the server
        """
        if self._authenticated:
            return

        if self.tls:
            login_request = LoginRequest22V5(
                user_id=self.username, password=self.password
            )

    async def _receive_response(self, response: RequestResult) -> CommandResult:
        """Receives response from requester and returns BWKSCommand"""

        if isinstance(response, MError):
            raise response

        response_dict = Parser.to_dict_from_xml(response)

        command_data = response_dict.get("command")

        if isinstance(command_data, dict):
            type_name: Union[str, None] = command_data.get("attributes", {}).get(
                "{http://www.w3.org/2001/XMLSchema-instance}type"
            )
        else:
            return SuccessResponse()

        if not type_name or not isinstance(type_name, str):
            raise MError("Failed to parse response object")

        if ":" in type_name:
            type_name = type_name.split(":", 1)[1]

        # Cache Response Class
        response_class = getattr(commands, type_name)

        # Validate Response Class Instantiation
        if not response_class:
            raise MError(f"Failed To Find Raw Response Type: {type_name}")

        # Construct Response Class With Raw Response
        return response_class.from_xml(response)

    def disconnect(self) -> Union[None, Awaitable[None]]:
        """Disconnects from the server

        Call this method at the end of your program to disconnect from the server.
        """
        pass

    def _set_up_logging(self):
        """Common logging setup for all clients"""
        logger = logging.getLogger(__name__)
        logger.setLevel(logging.WARNING)
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.WARNING)
        logger.addHandler(console_handler)
        return logger


class AsyncClient(BaseClient):
    """Async version of Client.

    Note: Performs the same functions as Client, but in an async manner.
    This Client needs authenticating manually. Call authenticate() before using.

    Args:
        host (str): URL or IP address of server. Depends on connection type. If SOAP DO NOT include '?wsdl' in the end of the URL.
        username (str): The username of the user
        password (str): The password of the user
        conn_type (str): Either 'TCP' or 'SOAP'. TCP is the default.

        port (int): The port of the server. Default is 2209. Only used in TCP mode.
        secure (bool): Whether the connection is secure. Default is True. Only used in TCP mode. Password is hashed if not secure.

        timeout (int): The timeout of the client. Default is 30 seconds.
        user_agent (str): The user agent of the client, used for logging. Default is 'Thor\'s Hammer'.
        logger (logging.Logger): The logger of the client. Default is None.

    Attributes:
        authenticated (bool): Whether the client is authenticated
        session_id (str): The session id of the client
        _dispatch_table (dict): The dispatch table of the client

    Raises:
        Exception: If the client fails to authenticate
    """

    _requester: Union[AsyncTCPRequester, AsyncSOAPRequester]  # type: ignore

    @property
    def async_mode(self) -> bool:
        return True

    def __attrs_post_init__(self):
        super().__attrs_post_init__()  # Call the BaseClient's post-init logic
        # The requester must be either SyncTCPRequester or SyncSOAPRequester,
        # not BaseRequester as the interpreter assumes synchronous calls can be
        # awaitable due to its base class.
        assert isinstance(self._requester, (AsyncTCPRequester, AsyncSOAPRequester))

    async def command(self, command: CommandInput) -> CommandResult:
        """
        Executes all requests to the server.
        If the client is not authenticated, it will authenticate first.

        Args:
            command (BWKSCommand): The command class to execute

        Returns:
            BWKSCommand: The response from the server
        """

        if not self.authenticated:
            await self.authenticate()
        self.logger.info(f"Executing command: {command.__class__.__name__}")
        self.logger.debug(f"Command: {await command.to_dict_async()}")
        response = await self._requester.send_request(await command.to_xml_async())
        return await self._receive_response(response)

    async def raw_command(self, command: str, **kwargs: str) -> CommandResult:
        """
        Executes raw command specified by end user - instantiates class command.

        Args:
            command (str): The command to execute
            **kwargs: The arguments to pass to the command

        Returns:
            BWKSCommand: The response from the server

        Raises:
            ValueError: If the command is not found in the dispatch table
        """
        command_class = self._dispatch_table.get(command)
        if not command_class:
            self.logger.error(f"Command {command} not found in dispatch table")
            raise ValueError(f"Command {command} not found in dispatch table")
        response = await self.command(command_class(**kwargs))
        return response

    async def authenticate(self) -> CommandResult:
        """
        Authenticates client with username and password in client.

        Note: Directly send request to requester to avoid double authentication

        Returns:
            BWKSCommand: The response from the server

        Raises:
            THError: If the command is not found in the dispatch table
        """
        # If client is already authenticated, return
        if self.authenticated:
            return

        if self.session_id == "":
            self.session_id = str(uuid.uuid4())

        # Default to 22V5 login request - recommended
        if not (login_request_class := self._dispatch_table.get("LoginRequest22V5")):
            raise ValueError("LoginRequest22V5 not found in dispatch table")
        request: BWKSCommand = login_request_class(
            user_id=self.username, password=self.password
        )

        if not self.tls:
            # Hashing password needed when not over secure connection

            if not (auth_request := self._dispatch_table.get("AuthenticationRequest")):
                raise ValueError("AuthenticationRequest not found in dispatch table")

            auth_resp: BWKSCommand | None = await self._receive_response(
                await self._requester.send_request(
                    await auth_request(user_id=self.username).to_xml_async()
                )
            )

            assert auth_resp is not None and hasattr(auth_resp, "nonce")

            authhash: str = hashlib.sha1(self.password.encode()).hexdigest().lower()
            signed_password: str = (
                hashlib.md5(":".join([auth_resp.nonce, authhash]).encode())  # type: ignore
                .hexdigest()
                .lower()
            )  # We can safely ignore the type here as we know auth_resp is a valid response with a nonce

            if not (
                login_request_class := self._dispatch_table.get("LoginRequest14sp4")
            ):
                raise ValueError("LoginRequest14sp4 not found in dispatch table")

            request = login_request_class(
                user_id=self.username, signed_password=signed_password
            )

        login_resp = await self._receive_response(
            await self._requester.send_request(await request.to_xml_async())
        )

        if isinstance(login_resp, BWKSErrorResponse):
            raise MError(f"Failed to authenticate: {login_resp.summary}")

        self.logger.info("Authenticated with server")
        self.authenticated = True
        return login_resp

    async def _receive_response(self, response: RequestResult) -> CommandResult:
        """Receives response from requester and returns BWKSCommand"""

        if isinstance(response, MError):
            raise response

        response_dict: XMLDictResult = await AsyncParser.to_dict_from_xml(response)

        # Check if response_dict is a dict before accessing
        if not isinstance(response_dict, dict):
            raise MError("Failed to parse response object - invalid format")

        command_data = response_dict.get("command")

        if isinstance(command_data, dict):
            type_name: Union[str, None] = command_data.get("attributes", {}).get(
                "{http://www.w3.org/2001/XMLSchema-instance}type"
            )
        else:
            return BWKSSucessResponse()

        # Validate Typename Extraction
        if not type_name or not isinstance(type_name, str):
            raise MError("Failed to parse response object")

        assert isinstance(type_name, str)
        # Remove Namespace From Typename
        if ":" in type_name:
            type_name = type_name.split(":", 1)[1]

        # Cache Response Class
        response_class = self._dispatch_table.get(f"{type_name}")

        # Validate Response Class Instantiation
        if not response_class:
            raise MError(f"Failed To Find Raw Response Type: {type_name}")

        # Construct Response Class With Raw Response
        return await response_class.from_xml_async(response)  # type: ignore

    async def disconnect(self) -> None:
        """Disconnects from the server

        Call this method at the end of your program to disconnect from the server.
        """
        self.authenticated = False
        self.session_id = ""
        await self._requester.disconnect()
