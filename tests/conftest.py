"""
Shared fixtures for mercury_ocip_fast tests.
"""

import pytest
from unittest.mock import Mock, AsyncMock


@pytest.fixture
def mock_logger():
    """Create a mock logger with common methods."""
    return Mock(spec=["info", "debug", "warning", "error", "setLevel", "addHandler"])


@pytest.fixture
def mock_stream_reader():
    """Create a mock asyncio StreamReader."""
    reader = AsyncMock()
    reader.read = AsyncMock(return_value=b"")
    return reader


@pytest.fixture
def mock_stream_writer():
    """Create a mock asyncio StreamWriter."""
    writer = AsyncMock()
    writer.close = Mock()
    writer.wait_closed = AsyncMock()
    writer.writelines = Mock()
    writer.drain = AsyncMock()
    return writer


@pytest.fixture
def sample_login_response_xml():
    """Sample login response XML for testing."""
    return '''<?xml version="1.0" encoding="ISO-8859-1"?>
<BroadsoftDocument protocol="OCI" xmlns="C" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<sessionId>test-session-id</sessionId>
<command xsi:type="c:LoginResponse22V5">
<loginType>System</loginType>
<locale>en_US</locale>
<encoding>ISO-8859-1</encoding>
</command>
</BroadsoftDocument>'''


@pytest.fixture
def sample_success_response_xml():
    """Sample success response XML for testing."""
    return '''<?xml version="1.0" encoding="ISO-8859-1"?>
<BroadsoftDocument protocol="OCI" xmlns="C" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<sessionId>test-session-id</sessionId>
<command xsi:type="c:SuccessResponse">
</command>
</BroadsoftDocument>'''


@pytest.fixture
def sample_error_response_xml():
    """Sample error response XML for testing."""
    return '''<?xml version="1.0" encoding="ISO-8859-1"?>
<BroadsoftDocument protocol="OCI" xmlns="C" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<sessionId>test-session-id</sessionId>
<command xsi:type="c:ErrorResponse">
<errorCode>4962</errorCode>
<summary>[Error 4962] Invalid userId or password.</summary>
<summaryEnglish>[Error 4962] Invalid userId or password.</summaryEnglish>
</command>
</BroadsoftDocument>'''


@pytest.fixture
def sample_auth_response_xml():
    """Sample authentication response XML for testing."""
    return '''<?xml version="1.0" encoding="ISO-8859-1"?>
<BroadsoftDocument protocol="OCI" xmlns="C" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<sessionId>test-session-id</sessionId>
<command xsi:type="c:AuthenticationResponse">
<userId>admin@example.com</userId>
<nonce>1234567890abcdef</nonce>
<passwordAlgorithm>MD5</passwordAlgorithm>
</command>
</BroadsoftDocument>'''
