"""Tests for mercury_ocip_fast.pool module."""

import pytest
import asyncio
import time
from unittest.mock import Mock, AsyncMock, patch, MagicMock

from mercury_ocip_fast.pool import PoolConfig, PooledConnection, TCPConnectionPool
from mercury_ocip_fast.exceptions import MErrorSocketInitialisation, MErrorSocketTimeout


class TestPoolConfig:
    """Tests for PoolConfig dataclass."""

    def test_default_values(self):
        """Test PoolConfig initializes with correct defaults."""
        config = PoolConfig()

        assert config.max_connections == 50
        assert config.max_concurrent_requests == 100
        assert config.connect_timeout == 10.0
        assert config.read_timeout == 30.0
        assert config.acquire_timeout == 5.0
        assert config.max_connection_age == 300.0
        assert config.idle_timeout == 60.0
        assert config.read_chunk_size == 8192

    def test_custom_values(self):
        """Test PoolConfig accepts custom values."""
        config = PoolConfig(
            max_connections=10,
            max_concurrent_requests=20,
            connect_timeout=5.0,
            read_timeout=15.0,
            acquire_timeout=2.0,
            max_connection_age=120.0,
            idle_timeout=30.0,
            read_chunk_size=4096,
        )

        assert config.max_connections == 10
        assert config.max_concurrent_requests == 20
        assert config.connect_timeout == 5.0
        assert config.read_timeout == 15.0
        assert config.acquire_timeout == 2.0
        assert config.max_connection_age == 120.0
        assert config.idle_timeout == 30.0
        assert config.read_chunk_size == 4096


class TestPooledConnection:
    """Tests for PooledConnection dataclass."""

    @pytest.fixture
    def mock_reader(self):
        return AsyncMock(spec=asyncio.StreamReader)

    @pytest.fixture
    def mock_writer(self):
        writer = AsyncMock(spec=asyncio.StreamWriter)
        writer.close = Mock()
        writer.wait_closed = AsyncMock()
        return writer

    @pytest.fixture
    def pooled_conn(self, mock_reader, mock_writer):
        return PooledConnection(reader=mock_reader, writer=mock_writer)

    def test_initialization(self, pooled_conn):
        """Test PooledConnection initializes with correct defaults."""
        assert pooled_conn.in_use is False
        assert pooled_conn.created_at > 0
        assert pooled_conn.last_used > 0

    def test_is_stale_returns_false_for_fresh_connection(self, pooled_conn):
        """Test is_stale returns False for newly created connection."""
        assert pooled_conn.is_stale(max_age_seconds=300.0) is False

    def test_is_stale_returns_true_for_old_connection(self, mock_reader, mock_writer):
        """Test is_stale returns True for connection exceeding max age."""
        conn = PooledConnection(reader=mock_reader, writer=mock_writer)
        # Manually set created_at to simulate old connection
        conn.created_at = time.monotonic() - 400
        assert conn.is_stale(max_age_seconds=300.0) is True

    def test_idle_time_for_fresh_connection(self, pooled_conn):
        """Test idle_time returns small value for fresh connection."""
        idle = pooled_conn.idle_time()
        assert idle < 1.0  # Should be nearly zero

    def test_idle_time_for_old_connection(self, mock_reader, mock_writer):
        """Test idle_time returns correct value for idle connection."""
        conn = PooledConnection(reader=mock_reader, writer=mock_writer)
        conn.last_used = time.monotonic() - 10
        idle = conn.idle_time()
        assert idle >= 10.0

    def test_touch_updates_last_used(self, pooled_conn):
        """Test touch() updates the last_used timestamp."""
        old_last_used = pooled_conn.last_used
        time.sleep(0.01)  # Small delay to ensure time difference
        pooled_conn.touch()
        assert pooled_conn.last_used > old_last_used

    @pytest.mark.asyncio
    async def test_close_calls_writer_close(self, pooled_conn, mock_writer):
        """Test close() properly closes the writer."""
        await pooled_conn.close()

        mock_writer.close.assert_called_once()
        mock_writer.wait_closed.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_handles_exception_gracefully(self, mock_reader, mock_writer):
        """Test close() handles exceptions without raising."""
        mock_writer.close.side_effect = Exception("Already closed")
        conn = PooledConnection(reader=mock_reader, writer=mock_writer)

        # Should not raise
        await conn.close()


class TestTCPConnectionPool:
    """Tests for TCPConnectionPool."""

    @pytest.fixture
    def mock_logger(self):
        return Mock(spec=["info", "debug", "warning", "error"])

    @pytest.fixture
    def pool_config(self):
        return PoolConfig(
            max_connections=5,
            max_concurrent_requests=10,
            connect_timeout=1.0,
            acquire_timeout=1.0,
        )

    @pytest.fixture
    def pool(self, mock_logger, pool_config):
        return TCPConnectionPool(
            host="localhost",
            port=2209,
            config=pool_config,
            tls=False,
            logger=mock_logger,
        )

    def test_initialization(self, pool, mock_logger):
        """Test pool initializes with correct attributes."""
        assert pool.host == "localhost"
        assert pool.port == 2209
        assert pool.tls is False
        assert pool._closed is False
        assert len(pool._all_connections) == 0
        mock_logger.info.assert_called()

    def test_stats_initial(self, pool):
        """Test stats returns correct initial values."""
        stats = pool.stats

        assert stats["total_connections"] == 0
        assert stats["available"] == 0
        assert stats["in_use"] == 0
        assert stats["waiting"] == 0
        assert stats["max_connections"] == 5
        assert stats["max_concurrent"] == 10

    def test_repr(self, pool):
        """Test __repr__ returns informative string."""
        repr_str = repr(pool)

        assert "TCPConnectionPool" in repr_str
        assert "localhost" in repr_str
        assert "2209" in repr_str

    @pytest.mark.asyncio
    async def test_create_conn_success(self, pool):
        """Test _create_conn successfully creates a connection."""
        mock_reader = AsyncMock()
        mock_writer = AsyncMock()

        with patch("asyncio.open_connection", return_value=(mock_reader, mock_writer)):
            conn = await pool._create_conn()

            assert isinstance(conn, PooledConnection)
            assert conn.reader == mock_reader
            assert conn.writer == mock_writer

    @pytest.mark.asyncio
    async def test_create_conn_timeout_raises_error(self, pool):
        """Test _create_conn raises MErrorSocketInitialisation on timeout."""
        with patch(
            "asyncio.open_connection", side_effect=asyncio.TimeoutError()
        ):
            with pytest.raises(MErrorSocketInitialisation) as exc_info:
                await pool._create_conn()

            assert "timeout" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_create_conn_os_error_raises_error(self, pool):
        """Test _create_conn raises MErrorSocketInitialisation on OSError."""
        with patch(
            "asyncio.open_connection", side_effect=OSError("Connection refused")
        ):
            with pytest.raises(MErrorSocketInitialisation) as exc_info:
                await pool._create_conn()

            assert "Connection" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_acquire_creates_new_connection(self, pool):
        """Test acquire creates a new connection when pool is empty."""
        mock_reader = AsyncMock()
        mock_writer = AsyncMock()
        mock_writer.close = Mock()
        mock_writer.wait_closed = AsyncMock()

        with patch("asyncio.open_connection", return_value=(mock_reader, mock_writer)):
            async with pool.acquire() as conn:
                assert isinstance(conn, PooledConnection)
                assert conn.in_use is True
                assert len(pool._all_connections) == 1

    @pytest.mark.asyncio
    async def test_acquire_reuses_pooled_connection(self, pool):
        """Test acquire reuses an existing connection from the pool."""
        mock_reader = AsyncMock()
        mock_writer = AsyncMock()
        mock_writer.close = Mock()
        mock_writer.wait_closed = AsyncMock()

        with patch("asyncio.open_connection", return_value=(mock_reader, mock_writer)):
            # First acquire creates a connection
            async with pool.acquire() as conn1:
                first_conn = conn1

            # Second acquire should reuse the same connection
            async with pool.acquire() as conn2:
                assert conn2 is first_conn

    @pytest.mark.asyncio
    async def test_acquire_discards_stale_connection(self, pool, pool_config):
        """Test acquire discards stale connections."""
        mock_reader = AsyncMock()
        mock_writer = AsyncMock()
        mock_writer.close = Mock()
        mock_writer.wait_closed = AsyncMock()

        # Create pool with very short max connection age
        pool_config.max_connection_age = 0.001

        with patch("asyncio.open_connection", return_value=(mock_reader, mock_writer)):
            async with pool.acquire() as conn1:
                pass

            # Wait for connection to become stale
            await asyncio.sleep(0.01)

            # Should get a new connection since the old one is stale
            async with pool.acquire() as conn2:
                assert conn2 is not conn1

    @pytest.mark.asyncio
    async def test_acquire_raises_when_closed(self, pool):
        """Test acquire raises RuntimeError when pool is closed."""
        pool._closed = True

        with pytest.raises(RuntimeError) as exc_info:
            async with pool.acquire():
                pass

        assert "closed" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_acquire_marks_connection_unhealthy_on_exception(self, pool):
        """Test connection is marked unhealthy when exception occurs during use."""
        mock_reader = AsyncMock()
        mock_writer = AsyncMock()
        mock_writer.close = Mock()
        mock_writer.wait_closed = AsyncMock()

        with patch("asyncio.open_connection", return_value=(mock_reader, mock_writer)):
            try:
                async with pool.acquire() as conn:
                    raise ValueError("Test error")
            except ValueError:
                pass

            # Connection should be removed from pool
            assert pool._pool.qsize() == 0

    @pytest.mark.asyncio
    async def test_return_connection_to_waiter(self, pool, mock_logger):
        """Test connection is given to waiting tasks."""
        mock_reader = AsyncMock()
        mock_writer = AsyncMock()
        mock_writer.close = Mock()
        mock_writer.wait_closed = AsyncMock()

        pool.config.max_connections = 1

        acquired_connections = []

        async def acquire_task():
            async with pool.acquire() as conn:
                acquired_connections.append(conn)
                await asyncio.sleep(0.05)

        with patch("asyncio.open_connection", return_value=(mock_reader, mock_writer)):
            # Start two tasks that both try to acquire
            task1 = asyncio.create_task(acquire_task())
            await asyncio.sleep(0.01)  # Let task1 acquire first
            task2 = asyncio.create_task(acquire_task())

            await asyncio.gather(task1, task2)

            # Both tasks should have acquired the same connection
            assert len(acquired_connections) == 2
            assert acquired_connections[0] is acquired_connections[1]

    @pytest.mark.asyncio
    async def test_warm_creates_connections(self, pool):
        """Test warm() pre-creates connections."""
        mock_reader = AsyncMock()
        mock_writer = AsyncMock()

        with patch("asyncio.open_connection", return_value=(mock_reader, mock_writer)):
            created = await pool.warm(count=3)

            assert created == 3
            assert len(pool._all_connections) == 3
            assert pool._pool.qsize() == 3

    @pytest.mark.asyncio
    async def test_warm_respects_max_connections(self, pool):
        """Test warm() doesn't exceed max_connections."""
        mock_reader = AsyncMock()
        mock_writer = AsyncMock()

        with patch("asyncio.open_connection", return_value=(mock_reader, mock_writer)):
            created = await pool.warm(count=100)

            assert created == pool.config.max_connections
            assert len(pool._all_connections) == pool.config.max_connections

    @pytest.mark.asyncio
    async def test_warm_handles_failed_connections(self, pool, mock_logger):
        """Test warm() handles connection failures gracefully."""
        call_count = 0

        async def flaky_connection(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count % 2 == 0:
                raise OSError("Connection failed")
            return (AsyncMock(), AsyncMock())

        with patch("asyncio.open_connection", side_effect=flaky_connection):
            created = await pool.warm(count=4)

            # Only half should succeed
            assert created == 2
            mock_logger.warning.assert_called()

    @pytest.mark.asyncio
    async def test_close_shuts_down_pool(self, pool):
        """Test close() properly shuts down the pool."""
        mock_reader = AsyncMock()
        mock_writer = AsyncMock()
        mock_writer.close = Mock()
        mock_writer.wait_closed = AsyncMock()

        with patch("asyncio.open_connection", return_value=(mock_reader, mock_writer)):
            await pool.warm(count=2)
            await pool.close()

            assert pool._closed is True
            assert len(pool._all_connections) == 0
            assert pool._pool.empty()

    @pytest.mark.asyncio
    async def test_close_cancels_waiters(self, pool):
        """Test close() cancels waiting tasks."""
        # Create a waiter
        waiter = asyncio.get_running_loop().create_future()
        pool._waiters.append(waiter)

        await pool.close()

        assert waiter.cancelled()
        assert len(pool._waiters) == 0

    @pytest.mark.asyncio
    async def test_acquire_timeout_when_pool_exhausted(self, pool, pool_config):
        """Test acquire times out when pool is exhausted."""
        mock_reader = AsyncMock()
        mock_writer = AsyncMock()
        mock_writer.close = Mock()
        mock_writer.wait_closed = AsyncMock()

        pool.config.max_connections = 1
        pool.config.acquire_timeout = 0.1

        with patch("asyncio.open_connection", return_value=(mock_reader, mock_writer)):
            async with pool.acquire():
                # While holding the only connection, try to acquire another
                with pytest.raises(MErrorSocketTimeout) as exc_info:
                    async with pool.acquire():
                        pass

                assert "Timeout" in str(exc_info.value)
