"""Tests for mercury_ocip_fast.client module."""

import pytest
import logging
from unittest.mock import Mock, AsyncMock, patch

from mercury_ocip_fast.client import Client, FakeDispatchTable
from mercury_ocip_fast.pool import PoolConfig
from mercury_ocip_fast.requester import AsyncTCPRequester
from mercury_ocip_fast.commands.base_command import (
    ErrorResponse,
    SuccessResponse,
    OCIResponse,
)
from mercury_ocip_fast.exceptions import MError


class MockCommand:
    """A mock command for testing."""

    def to_xml(self):
        return '<command xmlns="" xsi:type="MockCommand"><param>value</param></command>'


class MockResponse(OCIResponse):
    """A mock response for testing."""

    data: str = None


class TestFakeDispatchTable:
    """Tests for FakeDispatchTable backwards compatibility."""

    def test_get_existing_command(self):
        """Test getting an existing command class."""
        mock_client = Mock()
        table = FakeDispatchTable(mock_client)

        # Should return the actual class from commands module
        result = table.get("LoginRequest22V5")
        assert result is not None

    def test_get_nonexistent_command_returns_default(self):
        """Test getting a nonexistent command returns default."""
        mock_client = Mock()
        table = FakeDispatchTable(mock_client)

        result = table.get("NonExistentCommand", default="default_value")
        assert result == "default_value"


class TestClient:
    """Tests for Client class."""

    @pytest.fixture
    def mock_logger(self):
        return Mock(spec=["info", "debug", "warning", "error", "setLevel", "addHandler"])

    @pytest.fixture
    def pool_config(self):
        return PoolConfig(
            max_connections=5,
            connect_timeout=1.0,
            read_timeout=1.0,
        )

    @pytest.fixture
    def mock_requester(self):
        """Create a mock requester."""
        mock = Mock(spec=AsyncTCPRequester)
        mock.send_request = AsyncMock()
        mock.send_bulk_request = AsyncMock()
        mock.warm = AsyncMock()
        mock.close = AsyncMock()
        return mock

    @pytest.fixture
    def client(self, mock_logger, pool_config, mock_requester):
        """Create a client with mocked requester."""
        with patch.object(AsyncTCPRequester, "__attrs_post_init__"):
            c = Client(
                host="localhost",
                port=2209,
                username="admin",
                password="password123",
                config=pool_config,
                logger=mock_logger,
                tls=True,
            )
            # Replace the requester with our mock
            object.__setattr__(c, "_requester", mock_requester)
            return c

    def test_initialization_defaults(self):
        """Test client initializes with correct defaults."""
        with patch.object(AsyncTCPRequester, "__attrs_post_init__"):
            client = Client(
                host="example.com",
                username="user",
                password="pass",
            )

            assert client.host == "example.com"
            assert client.port == 2209
            assert client.username == "user"
            assert client.password == "pass"
            assert client.tls is True
            assert client.user_agent == "Broadworks SDK"
            assert client.session_id is not None
            assert client._authenticated is False

    def test_initialization_custom_values(self, mock_logger, pool_config):
        """Test client initializes with custom values."""
        with patch.object(AsyncTCPRequester, "__attrs_post_init__"):
            client = Client(
                host="custom.example.com",
                port=2208,
                username="custom_user",
                password="custom_pass",
                config=pool_config,
                user_agent="Custom Agent",
                session_id="custom-session-id",
                tls=False,
                logger=mock_logger,
            )

            assert client.host == "custom.example.com"
            assert client.port == 2208
            assert client.username == "custom_user"
            assert client.password == "custom_pass"
            assert client.tls is False
            assert client.user_agent == "Custom Agent"
            assert client.session_id == "custom-session-id"

    def test_getattr_returns_fake_dispatch_table(self, client):
        """Test __getattr__ returns FakeDispatchTable for _dispatch_table."""
        result = client._dispatch_table
        assert isinstance(result, FakeDispatchTable)

    def test_getattr_raises_for_unknown_attribute(self, client):
        """Test __getattr__ raises AttributeError for unknown attributes."""
        with pytest.raises(AttributeError):
            _ = client.nonexistent_attribute

    @pytest.mark.asyncio
    async def test_authenticate_tls_success(self, mock_logger, pool_config):
        """Test TLS authentication succeeds."""
        mock_req = Mock(spec=AsyncTCPRequester)
        # Return a valid login response XML
        mock_req.send_request = AsyncMock(return_value='''<?xml version="1.0"?>
<BroadsoftDocument xmlns="C" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<command xsi:type="c:LoginResponse22V5">
<loginType>System</loginType>
<locale>en_US</locale>
<encoding>ISO-8859-1</encoding>
<isEnterprise>false</isEnterprise>
<userDomain>example.com</userDomain>
</command>
</BroadsoftDocument>''')

        with patch.object(AsyncTCPRequester, "__attrs_post_init__"):
            client = Client(
                host="localhost",
                username="admin",
                password="password123",
                config=pool_config,
                logger=mock_logger,
                tls=True,
            )
            object.__setattr__(client, "_requester", mock_req)

        await client.authenticate()

        assert client._authenticated is True
        mock_req.send_request.assert_called_once()
        mock_logger.info.assert_called()

    @pytest.mark.asyncio
    async def test_authenticate_tls_failure(self, mock_logger, pool_config):
        """Test TLS authentication failure raises MError."""
        mock_req = Mock(spec=AsyncTCPRequester)
        # Return an error response XML
        mock_req.send_request = AsyncMock(return_value='''<?xml version="1.0"?>
<BroadsoftDocument xmlns="C" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<command xsi:type="c:ErrorResponse">
<errorCode>4962</errorCode>
<summary>[Error 4962] Invalid userId or password.</summary>
<summaryEnglish>[Error 4962] Invalid userId or password.</summaryEnglish>
</command>
</BroadsoftDocument>''')

        with patch.object(AsyncTCPRequester, "__attrs_post_init__"):
            client = Client(
                host="localhost",
                username="admin",
                password="wrong_password",
                config=pool_config,
                logger=mock_logger,
                tls=True,
            )
            object.__setattr__(client, "_requester", mock_req)

        with pytest.raises(MError) as exc_info:
            await client.authenticate()

        assert "authenticate" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_authenticate_non_tls_success(self, mock_logger, pool_config):
        """Test non-TLS two-stage authentication succeeds."""
        mock_req = Mock(spec=AsyncTCPRequester)

        auth_response_xml = '''<?xml version="1.0"?>
<BroadsoftDocument xmlns="C" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<command xsi:type="c:AuthenticationResponse">
<userId>user</userId>
<nonce>test-nonce-12345</nonce>
<passwordAlgorithm>MD5</passwordAlgorithm>
</command>
</BroadsoftDocument>'''

        login_response_xml = '''<?xml version="1.0"?>
<BroadsoftDocument xmlns="C" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<command xsi:type="c:LoginResponse14sp4">
<loginType>System</loginType>
<locale>en_US</locale>
<encoding>ISO-8859-1</encoding>
</command>
</BroadsoftDocument>'''

        call_count = 0

        async def mock_send(_cmd):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return auth_response_xml
            return login_response_xml

        mock_req.send_request = AsyncMock(side_effect=mock_send)

        with patch.object(AsyncTCPRequester, "__attrs_post_init__"):
            client = Client(
                host="localhost",
                username="user",
                password="password",
                config=pool_config,
                logger=mock_logger,
                tls=False,
            )
            object.__setattr__(client, "_requester", mock_req)

        await client.authenticate()

        assert client._authenticated is True
        # Should be called twice: AuthenticationRequest and LoginRequest14sp4
        assert mock_req.send_request.call_count == 2

    @pytest.mark.asyncio
    async def test_authenticate_skips_if_already_authenticated(self, client):
        """Test authenticate returns None if already authenticated."""
        object.__setattr__(client, "_authenticated", True)

        result = await client.authenticate()

        assert result is None

    @pytest.mark.asyncio
    async def test_command_authenticates_first(self, mock_logger, pool_config):
        """Test command() calls authenticate if not authenticated."""
        mock_req = Mock(spec=AsyncTCPRequester)

        # First call is login, second is command
        call_count = 0

        async def mock_send(_cmd):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Login response
                return '''<?xml version="1.0"?>
<BroadsoftDocument xmlns="C" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<command xsi:type="c:LoginResponse22V5">
<loginType>System</loginType>
<locale>en_US</locale>
<encoding>ISO-8859-1</encoding>
<isEnterprise>false</isEnterprise>
<userDomain>example.com</userDomain>
</command>
</BroadsoftDocument>'''
            # Command response (no command element = SuccessResponse)
            return '''<?xml version="1.0"?>
<BroadsoftDocument xmlns="C" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
</BroadsoftDocument>'''

        mock_req.send_request = AsyncMock(side_effect=mock_send)

        with patch.object(AsyncTCPRequester, "__attrs_post_init__"):
            client = Client(
                host="localhost",
                username="admin",
                password="password",
                config=pool_config,
                logger=mock_logger,
            )
            object.__setattr__(client, "_requester", mock_req)

        mock_command = MockCommand()
        await client.command(mock_command)

        # Should have been called twice: once for auth, once for command
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_command_single(self, mock_logger, pool_config):
        """Test command() with single command."""
        mock_req = Mock(spec=AsyncTCPRequester)
        # No command element = implicit SuccessResponse
        mock_req.send_request = AsyncMock(return_value='''<?xml version="1.0"?>
<BroadsoftDocument xmlns="C" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
</BroadsoftDocument>''')

        with patch.object(AsyncTCPRequester, "__attrs_post_init__"):
            client = Client(
                host="localhost",
                username="admin",
                password="password",
                config=pool_config,
                logger=mock_logger,
            )
            object.__setattr__(client, "_requester", mock_req)
            object.__setattr__(client, "_authenticated", True)

        mock_command = MockCommand()
        result = await client.command(mock_command)

        mock_req.send_request.assert_called_once()
        assert isinstance(result, SuccessResponse)

    @pytest.mark.asyncio
    async def test_command_bulk(self, mock_logger, pool_config):
        """Test command() with list of commands."""
        mock_req = Mock(spec=AsyncTCPRequester)
        # Multiple empty responses = multiple SuccessResponses
        mock_req.send_bulk_request = AsyncMock(return_value=[
            '''<?xml version="1.0"?>
<BroadsoftDocument xmlns="C" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
</BroadsoftDocument>''',
            '''<?xml version="1.0"?>
<BroadsoftDocument xmlns="C" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
</BroadsoftDocument>''',
            '''<?xml version="1.0"?>
<BroadsoftDocument xmlns="C" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
</BroadsoftDocument>'''
        ])

        with patch.object(AsyncTCPRequester, "__attrs_post_init__"):
            client = Client(
                host="localhost",
                username="admin",
                password="password",
                config=pool_config,
                logger=mock_logger,
            )
            object.__setattr__(client, "_requester", mock_req)
            object.__setattr__(client, "_authenticated", True)

        commands = [MockCommand(), MockCommand(), MockCommand()]
        result = await client.command(commands)

        mock_req.send_bulk_request.assert_called_once()
        assert isinstance(result, list)
        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_warm_delegates_to_requester(self, client, mock_requester):
        """Test warm() delegates to requester."""
        mock_requester.warm.return_value = 10
        result = await client.warm(connection_amount=10)

        assert result == 10
        mock_requester.warm.assert_called_once_with(10)

    @pytest.mark.asyncio
    async def test_disconnect(self, client, mock_requester):
        """Test disconnect() resets state and closes requester."""
        object.__setattr__(client, "_authenticated", True)
        client.session_id = "test-session"

        await client.disconnect()

        assert client._authenticated is False
        assert client.session_id == ""
        mock_requester.close.assert_called_once()

    def test_receive_response_raises_merror(self, client):
        """Test _receive_response re-raises MError."""
        error = MError(message="Test error")

        with pytest.raises(MError):
            client._receive_response(error)

    def test_receive_response_single_string(self, client):
        """Test _receive_response handles single string response."""
        # No command element = implicit SuccessResponse
        response_xml = '''<?xml version="1.0"?>
<BroadsoftDocument xmlns="C" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
</BroadsoftDocument>'''
        result = client._receive_response(response_xml)

        assert isinstance(result, SuccessResponse)

    def test_receive_response_list(self, client):
        """Test _receive_response handles list of responses."""
        # No command element = implicit SuccessResponse
        response_xml = '''<?xml version="1.0"?>
<BroadsoftDocument xmlns="C" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
</BroadsoftDocument>'''
        result = client._receive_response([response_xml, response_xml])

        assert len(result) == 2
        assert all(isinstance(r, SuccessResponse) for r in result)

    def test_receive_response_unexpected_type(self, client):
        """Test _receive_response raises MError for unexpected types."""
        with pytest.raises(MError):
            client._receive_response(12345)  # Unexpected type

    def test_parse_response_success_response(self, client):
        """Test _parse_response returns SuccessResponse for empty command."""
        with patch(
            "mercury_ocip_fast.client.Parser.to_dict_from_xml"
        ) as mock_parser:
            mock_parser.return_value = {}
            result = client._parse_response("<response/>")

            assert isinstance(result, SuccessResponse)

    def test_parse_response_error_response(self, client):
        """Test _parse_response handles ErrorResponse."""
        with patch(
            "mercury_ocip_fast.client.Parser.to_dict_from_xml"
        ) as mock_parser:
            mock_parser.return_value = {
                "command": {
                    "attributes": {
                        "{http://www.w3.org/2001/XMLSchema-instance}type": "ErrorResponse"
                    },
                    "summary": "Error message",
                    "summaryEnglish": "Error message",
                    "errorCode": 100,
                }
            }

            result = client._parse_response("<error/>")

            assert isinstance(result, ErrorResponse)

    def test_parse_response_multiple_commands(self, client):
        """Test _parse_response handles multiple commands in response."""
        # Use ErrorResponse which is handled explicitly
        response_xml = '''<?xml version="1.0"?>
<BroadsoftDocument xmlns="C" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<command xsi:type="c:ErrorResponse">
<errorCode>100</errorCode>
<summary>Error 1</summary>
<summaryEnglish>Error 1</summaryEnglish>
</command>
<command xsi:type="c:ErrorResponse">
<errorCode>200</errorCode>
<summary>Error 2</summary>
<summaryEnglish>Error 2</summaryEnglish>
</command>
</BroadsoftDocument>'''

        result = client._parse_response(response_xml)

        assert isinstance(result, list)
        assert len(result) == 2
        assert all(isinstance(r, ErrorResponse) for r in result)

    def test_parse_single_command_missing_type(self, client):
        """Test _parse_single_command raises MError when type is missing."""
        command_data = {"attributes": {}}

        with pytest.raises(MError):
            client._parse_single_command(command_data)

    def test_parse_single_command_unknown_type(self, client):
        """Test _parse_single_command raises MError for unknown command type."""
        command_data = {
            "attributes": {
                "{http://www.w3.org/2001/XMLSchema-instance}type": "UnknownResponseType"
            }
        }

        with pytest.raises(MError) as exc_info:
            client._parse_single_command(command_data)

        assert "UnknownResponseType" in str(exc_info.value)

    def test_parse_single_command_strips_namespace_prefix(self, client):
        """Test _parse_single_command handles namespace prefix in type."""
        command_data = {
            "attributes": {
                "{http://www.w3.org/2001/XMLSchema-instance}type": "c:ErrorResponse"
            },
            "summary": "Test error",
            "summaryEnglish": "Test error",
        }

        result = client._parse_single_command(command_data)

        assert isinstance(result, ErrorResponse)

    def test_set_up_logging_creates_logger(self):
        """Test _set_up_logging creates a properly configured logger."""
        with patch.object(AsyncTCPRequester, "__attrs_post_init__"):
            client = Client(
                host="localhost",
                username="user",
                password="pass",
            )

            assert isinstance(client.logger, logging.Logger)


class TestClientIntegration:
    """Integration-style tests for Client."""

    @pytest.fixture
    def mock_logger(self):
        return Mock(spec=["info", "debug", "warning", "error", "setLevel", "addHandler"])

    @pytest.mark.asyncio
    async def test_full_authentication_and_command_flow(self, mock_logger):
        """Test complete authentication and command execution flow."""
        mock_req = Mock(spec=AsyncTCPRequester)

        login_response_xml = '''<?xml version="1.0"?>
<BroadsoftDocument xmlns="C" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<sessionId>test-session</sessionId>
<command xsi:type="c:LoginResponse22V5">
<loginType>System</loginType>
<locale>en_US</locale>
<encoding>ISO-8859-1</encoding>
<isEnterprise>false</isEnterprise>
<userDomain>example.com</userDomain>
</command>
</BroadsoftDocument>'''

        command_response_xml = '''<?xml version="1.0"?>
<BroadsoftDocument xmlns="C" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<sessionId>test-session</sessionId>
<command xsi:type="c:SuccessResponse">
</command>
</BroadsoftDocument>'''

        call_count = 0

        async def mock_send(_cmd):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return login_response_xml
            return command_response_xml

        mock_req.send_request = AsyncMock(side_effect=mock_send)

        with patch.object(AsyncTCPRequester, "__attrs_post_init__"):
            client = Client(
                host="test.example.com",
                username="admin",
                password="password",
                logger=mock_logger,
            )
            object.__setattr__(client, "_requester", mock_req)

        # Execute a command (should authenticate first)
        mock_cmd = MockCommand()
        await client.command(mock_cmd)

        assert client._authenticated is True
        # First call is login, second is command
        assert call_count == 2
