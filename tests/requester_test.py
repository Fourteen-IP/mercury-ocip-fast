"""Tests for mercury_ocip_fast.requester module."""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from contextlib import asynccontextmanager

from mercury_ocip_fast.requester import AsyncTCPRequester
from mercury_ocip_fast.pool import PoolConfig, PooledConnection
from mercury_ocip_fast.exceptions import MError, MErrorSocketTimeout


class TestAsyncTCPRequester:
    """Tests for AsyncTCPRequester."""

    @pytest.fixture
    def mock_logger(self):
        return Mock(spec=["info", "debug", "warning", "error"])

    @pytest.fixture
    def pool_config(self):
        return PoolConfig(
            max_connections=5,
            connect_timeout=1.0,
            read_timeout=1.0,
            read_chunk_size=4096,
        )

    @pytest.fixture
    def mock_pool(self):
        """Create a mock pool."""
        pool = Mock()
        pool.warm = AsyncMock()
        pool.close = AsyncMock()
        pool.acquire = MagicMock()
        return pool

    @pytest.fixture
    def requester(self, mock_logger, pool_config):
        return AsyncTCPRequester(
            host="localhost",
            port=2209,
            config=pool_config,
            tls=False,
            session_id="test-session-123",
            logger=mock_logger,
        )

    @pytest.fixture
    def requester_with_mock_pool(self, mock_logger, pool_config, mock_pool):
        """Create a requester with a mocked pool."""
        req = AsyncTCPRequester(
            host="localhost",
            port=2209,
            config=pool_config,
            tls=False,
            session_id="test-session-123",
            logger=mock_logger,
        )
        object.__setattr__(req, "_pool", mock_pool)
        return req

    def test_initialization(self, requester, mock_logger):
        """Test requester initializes with correct attributes."""
        assert requester.host == "localhost"
        assert requester.port == 2209
        assert requester.tls is False
        assert requester.session_id == "test-session-123"
        assert requester._pool is not None
        assert requester._session_id_bytes == b"test-session-123"
        mock_logger.info.assert_called()

    def test_initialization_with_tls(self, mock_logger, pool_config):
        """Test requester initializes with TLS enabled."""
        requester = AsyncTCPRequester(
            host="secure.example.com",
            port=2209,
            config=pool_config,
            tls=True,
            session_id="secure-session",
            logger=mock_logger,
        )

        assert requester.tls is True
        assert requester._pool.tls is True

    def test_build_oci_xml_single_command(self, requester):
        """Test _build_oci_xml creates correct XML for single command."""
        command = '<command xmlns="" xsi:type="TestCommand"><param>value</param></command>'
        result = requester._build_oci_xml(command)

        assert isinstance(result, bytes)
        assert b'<?xml version="1.0" encoding="ISO-8859-1"?>' in result
        assert b'<BroadsoftDocument protocol="OCI"' in result
        assert b"<sessionId" in result
        assert b"test-session-123" in result
        assert b"TestCommand" in result
        assert b"</BroadsoftDocument>" in result

    def test_build_oci_xml_multiple_commands(self, requester):
        """Test _build_oci_xml creates correct XML for multiple commands."""
        commands = [
            '<command xmlns="" xsi:type="Command1"></command>',
            '<command xmlns="" xsi:type="Command2"></command>',
        ]
        result = requester._build_oci_xml(commands)

        assert isinstance(result, bytes)
        assert b"Command1" in result
        assert b"Command2" in result
        # Commands should be joined with newline
        assert result.count(b"<command") == 2

    def test_build_oci_xml_encodes_special_characters(self, requester):
        """Test _build_oci_xml handles ISO-8859-1 encoding."""
        command = '<command><param>café</param></command>'
        result = requester._build_oci_xml(command)

        assert isinstance(result, bytes)
        assert "café".encode("ISO-8859-1") in result

    @pytest.mark.asyncio
    async def test_send_request_success(self, requester_with_mock_pool, mock_pool):
        """Test send_request successfully sends and receives data."""
        mock_reader = AsyncMock()
        mock_writer = AsyncMock()
        mock_writer.writelines = Mock()
        mock_writer.drain = AsyncMock()

        # Simulate response chunks
        mock_reader.read = AsyncMock(
            side_effect=[
                b'<?xml version="1.0"?><BroadsoftDocument>',
                b'<command xsi:type="TestResponse"/></BroadsoftDocument>',
            ]
        )

        mock_conn = MagicMock(spec=PooledConnection)
        mock_conn.reader = mock_reader
        mock_conn.writer = mock_writer

        @asynccontextmanager
        async def mock_acquire(existing_conn=None):
            yield mock_conn

        mock_pool.acquire = mock_acquire

        result = await requester_with_mock_pool.send_request("<command>test</command>")

        assert isinstance(result, str)
        assert "BroadsoftDocument" in result
        assert "TestResponse" in result
        mock_writer.writelines.assert_called_once()
        mock_writer.drain.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_request_timeout(self, requester_with_mock_pool, mock_pool):
        """Test send_request raises MErrorSocketTimeout on read timeout."""
        mock_reader = AsyncMock()
        mock_writer = AsyncMock()
        mock_writer.writelines = Mock()
        mock_writer.drain = AsyncMock()
        mock_reader.read = AsyncMock(side_effect=asyncio.TimeoutError())

        mock_conn = MagicMock(spec=PooledConnection)
        mock_conn.reader = mock_reader
        mock_conn.writer = mock_writer

        @asynccontextmanager
        async def mock_acquire(existing_conn=None):
            yield mock_conn

        mock_pool.acquire = mock_acquire

        with pytest.raises(MErrorSocketTimeout):
            await requester_with_mock_pool.send_request("<command>test</command>")

    @pytest.mark.asyncio
    async def test_send_request_pool_not_initialized(self, requester):
        """Test send_request raises MError when pool is None."""
        requester._pool = None

        with pytest.raises(MError) as exc_info:
            await requester.send_request("<command>test</command>")

        assert "Pool failed" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_send_bulk_request_batches_commands(self, requester_with_mock_pool, mock_pool, mock_logger):
        """Test send_bulk_request batches commands correctly."""
        mock_reader = AsyncMock()
        mock_writer = AsyncMock()
        mock_writer.writelines = Mock()
        mock_writer.drain = AsyncMock()
        mock_reader.read = AsyncMock(
            return_value=b'<BroadsoftDocument><command/></BroadsoftDocument>'
        )

        mock_conn = MagicMock(spec=PooledConnection)
        mock_conn.reader = mock_reader
        mock_conn.writer = mock_writer

        @asynccontextmanager
        async def mock_acquire(existing_conn=None):
            yield mock_conn

        mock_pool.acquire = mock_acquire

        commands = [f"<command{i}/>" for i in range(25)]
        results = await requester_with_mock_pool.send_bulk_request(commands, batch_size=10)

        # 25 commands with batch_size=10 should create 3 batches
        assert len(results) == 3
        mock_logger.info.assert_called()

    @pytest.mark.asyncio
    async def test_send_bulk_request_preserves_order(self, requester_with_mock_pool, mock_pool):
        """Test send_bulk_request preserves response order."""
        call_count = 0
        mock_reader = AsyncMock()
        mock_writer = AsyncMock()
        mock_writer.writelines = Mock()
        mock_writer.drain = AsyncMock()

        async def read_response(_size):
            nonlocal call_count
            call_count += 1
            return f"<BroadsoftDocument><response batch={call_count}/></BroadsoftDocument>".encode()

        mock_reader.read = read_response

        mock_conn = MagicMock(spec=PooledConnection)
        mock_conn.reader = mock_reader
        mock_conn.writer = mock_writer

        @asynccontextmanager
        async def mock_acquire(existing_conn=None):
            yield mock_conn

        mock_pool.acquire = mock_acquire

        commands = ["<cmd1/>", "<cmd2/>", "<cmd3/>"]
        results = await requester_with_mock_pool.send_bulk_request(commands, batch_size=1)

        assert len(results) == 3
        assert "batch=1" in results[0]
        assert "batch=2" in results[1]
        assert "batch=3" in results[2]

    @pytest.mark.asyncio
    async def test_send_bulk_request_default_batch_size(self, requester_with_mock_pool, mock_pool):
        """Test send_bulk_request uses default batch size of 15."""
        mock_reader = AsyncMock()
        mock_writer = AsyncMock()
        mock_writer.writelines = Mock()
        mock_writer.drain = AsyncMock()
        mock_reader.read = AsyncMock(
            return_value=b'<BroadsoftDocument><command/></BroadsoftDocument>'
        )

        mock_conn = MagicMock(spec=PooledConnection)
        mock_conn.reader = mock_reader
        mock_conn.writer = mock_writer

        call_count = 0

        @asynccontextmanager
        async def mock_acquire(existing_conn=None):
            nonlocal call_count
            call_count += 1
            yield mock_conn

        mock_pool.acquire = mock_acquire

        # 30 commands should create 2 batches with default size of 15
        commands = [f"<cmd{i}/>" for i in range(30)]
        await requester_with_mock_pool.send_bulk_request(commands)

        assert call_count == 2

    @pytest.mark.asyncio
    async def test_warm_delegates_to_pool(self, requester_with_mock_pool, mock_pool):
        """Test warm() delegates to pool.warm()."""
        mock_pool.warm.return_value = 5
        result = await requester_with_mock_pool.warm(count=5)

        assert result == 5
        mock_pool.warm.assert_called_once_with(5)

    @pytest.mark.asyncio
    async def test_warm_returns_zero_when_pool_none(self, requester):
        """Test warm() returns 0 when pool is None."""
        requester._pool = None
        result = await requester.warm(count=5)

        assert result == 0

    @pytest.mark.asyncio
    async def test_close_delegates_to_pool(self, requester_with_mock_pool, mock_pool, mock_logger):
        """Test close() delegates to pool.close()."""
        await requester_with_mock_pool.close()

        mock_pool.close.assert_called_once()
        mock_logger.debug.assert_called()

    @pytest.mark.asyncio
    async def test_close_handles_exception(self, requester_with_mock_pool, mock_pool, mock_logger):
        """Test close() handles pool close exceptions gracefully."""
        mock_pool.close.side_effect = Exception("Close failed")

        # Should not raise
        await requester_with_mock_pool.close()

        mock_logger.warning.assert_called()

    @pytest.mark.asyncio
    async def test_send_bytes_reads_until_document_end(self, requester_with_mock_pool, mock_pool):
        """Test _send_bytes reads chunks until </BroadsoftDocument> is found."""
        mock_reader = AsyncMock()
        mock_writer = AsyncMock()
        mock_writer.writelines = Mock()
        mock_writer.drain = AsyncMock()

        # Simulate response split across multiple chunks
        mock_reader.read = AsyncMock(
            side_effect=[
                b'<?xml version="1.0"?>',
                b"<BroadsoftDocument>",
                b"<sessionId>123</sessionId>",
                b"<command/>",
                b"</BroadsoftDocument>",
            ]
        )

        mock_conn = MagicMock(spec=PooledConnection)
        mock_conn.reader = mock_reader
        mock_conn.writer = mock_writer

        @asynccontextmanager
        async def mock_acquire(existing_conn=None):
            yield mock_conn

        mock_pool.acquire = mock_acquire

        result = await requester_with_mock_pool._send_bytes(b"<test/>")

        assert "BroadsoftDocument" in result
        assert "sessionId" in result
        assert result.endswith("</BroadsoftDocument>")

    @pytest.mark.asyncio
    async def test_send_bytes_handles_empty_chunks(self, requester_with_mock_pool, mock_pool):
        """Test _send_bytes stops on empty chunk (connection closed)."""
        mock_reader = AsyncMock()
        mock_writer = AsyncMock()
        mock_writer.writelines = Mock()
        mock_writer.drain = AsyncMock()

        # Simulate connection closing before complete response
        mock_reader.read = AsyncMock(
            side_effect=[b"<partial>data", b""]
        )

        mock_conn = MagicMock(spec=PooledConnection)
        mock_conn.reader = mock_reader
        mock_conn.writer = mock_writer

        @asynccontextmanager
        async def mock_acquire(existing_conn=None):
            yield mock_conn

        mock_pool.acquire = mock_acquire

        result = await requester_with_mock_pool._send_bytes(b"<test/>")

        assert result == "<partial>data"


class TestAsyncTCPRequesterIntegration:
    """Integration-style tests for AsyncTCPRequester."""

    @pytest.fixture
    def mock_logger(self):
        return Mock(spec=["info", "debug", "warning", "error"])

    @pytest.mark.asyncio
    async def test_full_request_response_cycle(self, mock_logger):
        """Test a complete request-response cycle with mocked network."""
        requester = AsyncTCPRequester(
            host="test.example.com",
            port=2209,
            tls=False,
            session_id="integration-test-session",
            logger=mock_logger,
        )

        mock_reader = AsyncMock()
        mock_writer = AsyncMock()
        mock_writer.writelines = Mock()
        mock_writer.drain = AsyncMock()
        mock_writer.close = Mock()
        mock_writer.wait_closed = AsyncMock()

        response_xml = b'''<?xml version="1.0" encoding="ISO-8859-1"?>
<BroadsoftDocument protocol="OCI" xmlns="C">
<sessionId>integration-test-session</sessionId>
<command xsi:type="LoginResponse22V5" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<loginType>System</loginType>
</command>
</BroadsoftDocument>'''

        mock_reader.read = AsyncMock(return_value=response_xml)

        with patch("asyncio.open_connection", return_value=(mock_reader, mock_writer)):
            command = '<command xmlns="" xsi:type="LoginRequest22V5"><userId>admin</userId></command>'
            result = await requester.send_request(command)

            assert "LoginResponse22V5" in result
            assert "loginType" in result

        await requester.close()
