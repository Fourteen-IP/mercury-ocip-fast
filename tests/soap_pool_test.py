"""Tests for mercury_ocip_fast.soap_pool (SOAPSession + SOAPSessionPool)."""

import asyncio
import time
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from mercury_ocip_fast.exceptions import MErrorSocketTimeout
from mercury_ocip_fast.pool import SOAPPoolConfig
from mercury_ocip_fast.soap_pool import SOAPSession, SOAPSessionPool


# --------------------------------------------------------------------------- #
# Helpers / fixtures
# --------------------------------------------------------------------------- #


def make_session() -> SOAPSession:
    """Build a SOAPSession backed by a fake (closeable) httpx client."""
    http = Mock()
    http.is_closed = False
    http.aclose = AsyncMock()
    return SOAPSession(zeep_client=Mock(), http_client=http)


@pytest.fixture
def mock_logger():
    return Mock(spec=["info", "debug", "warning", "error"])


@pytest.fixture
def pool_builder(mock_logger, monkeypatch):
    """Return a builder that creates a SOAPSessionPool whose _create_session is
    stubbed to hand out fake sessions (no real zeep/httpx)."""
    created: list[SOAPSession] = []

    def _build(auth_callback=None, **config_kwargs) -> SOAPSessionPool:
        async def fake_create(self) -> SOAPSession:
            session = make_session()
            created.append(session)
            return session

        monkeypatch.setattr(SOAPSessionPool, "_create_session", fake_create)
        return SOAPSessionPool(
            host="https://bw.example.com/webservice/Service",
            config=SOAPPoolConfig(**config_kwargs),
            logger=mock_logger,
            auth_callback=auth_callback,
        )

    _build.created = created
    return _build


# --------------------------------------------------------------------------- #
# SOAPSession
# --------------------------------------------------------------------------- #


class TestSOAPSession:
    def test_defaults_have_unique_ids(self):
        a, b = make_session(), make_session()
        assert a.session_id != b.session_id
        assert isinstance(a.session_id, str)
        assert a.in_use is False

    def test_is_stale(self):
        session = make_session()
        assert session.is_stale(60.0) is False
        session.created_at = time.monotonic() - 120
        assert session.is_stale(60.0) is True

    def test_idle_time_and_touch(self):
        session = make_session()
        session.last_used = time.monotonic() - 30
        assert session.idle_time() >= 30
        session.touch()
        assert session.idle_time() < 1

    def test_is_healthy_tracks_http_client(self):
        session = make_session()
        assert session.is_healthy() is True
        session.http_client.is_closed = True
        assert session.is_healthy() is False

    @pytest.mark.asyncio
    async def test_close_closes_http_client(self):
        session = make_session()
        await session.close()
        session.http_client.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_swallows_errors(self):
        session = make_session()
        session.http_client.aclose = AsyncMock(side_effect=RuntimeError("boom"))
        await session.close()  # must not raise

    def test_jsessionid_none_before_login(self):
        session = make_session()
        session.http_client.cookies = httpx.Cookies()
        assert session.jsessionid is None

    def test_adopt_identity_sets_both_halves_of_the_pair(self):
        session = make_session()
        session.http_client.cookies = httpx.Cookies()
        session.adopt_identity("COOKIE-VALUE", "oci-session-id")
        assert session.jsessionid == "COOKIE-VALUE"
        assert session.session_id == "oci-session-id"


# --------------------------------------------------------------------------- #
# SOAPSessionPool
# --------------------------------------------------------------------------- #


class TestSOAPSessionPool:
    def test_post_init_sets_semaphore(self, pool_builder):
        pool = pool_builder(pool_size=5)
        assert pool._semaphore._value == 5

    @pytest.mark.asyncio
    async def test_acquire_creates_and_authenticates(self, pool_builder):
        auth = AsyncMock()
        pool = pool_builder(auth_callback=auth, pool_size=2)

        async with pool.acquire() as session:
            assert session.in_use is True
            assert session in pool._all_sessions

        auth.assert_awaited_once_with(session)
        # Returned to the pool, ready for reuse.
        assert session.in_use is False
        assert pool.stats["available"] == 1
        assert pool.session_ids == [session.session_id]

    @pytest.mark.asyncio
    async def test_acquire_reuses_pooled_session(self, pool_builder):
        pool = pool_builder(pool_size=2)

        async with pool.acquire() as first:
            pass
        async with pool.acquire() as second:
            pass

        assert first is second
        assert len(pool_builder.created) == 1  # only created once

    @pytest.mark.asyncio
    async def test_existing_session_bypasses_pool(self, pool_builder):
        auth = AsyncMock()
        pool = pool_builder(auth_callback=auth, pool_size=2)
        existing = make_session()

        async with pool.acquire(existing_session=existing) as session:
            assert session is existing

        auth.assert_not_awaited()
        assert pool_builder.created == []  # never created/tracked
        assert existing not in pool._all_sessions

    @pytest.mark.asyncio
    async def test_stale_session_discarded_on_checkout(self, pool_builder):
        pool = pool_builder(pool_size=2, max_session_age=60.0)
        async with pool.acquire() as first:
            first_id = first.session_id
        first.created_at = time.monotonic() - 120  # make it stale

        async with pool.acquire() as second:
            assert second.session_id != first_id

        # Stale sessions are closed in a fire-and-forget task; let it run.
        await asyncio.sleep(0.01)
        first.http_client.aclose.assert_awaited()

    @pytest.mark.asyncio
    async def test_idle_session_discarded_on_checkout(self, pool_builder):
        pool = pool_builder(pool_size=2, idle_timeout=30.0)
        async with pool.acquire() as first:
            pass
        first.last_used = time.monotonic() - 120  # exceed idle timeout

        async with pool.acquire() as second:
            assert second is not first

    @pytest.mark.asyncio
    async def test_unhealthy_session_discarded_on_checkout(self, pool_builder):
        pool = pool_builder(pool_size=2)
        async with pool.acquire() as first:
            pass
        first.http_client.is_closed = True

        async with pool.acquire() as second:
            assert second is not first

    @pytest.mark.asyncio
    async def test_auth_callback_failure_closes_session_and_raises(self, pool_builder):
        auth = AsyncMock(side_effect=RuntimeError("login failed"))
        pool = pool_builder(auth_callback=auth, pool_size=2)

        with pytest.raises(RuntimeError, match="login failed"):
            async with pool.acquire():
                pass

        # The session was closed and never added to the pool.
        created = pool_builder.created[-1]
        created.http_client.aclose.assert_awaited()
        assert pool._all_sessions == []
        # Semaphore permit released despite the failure.
        assert pool._semaphore._value == 2

    @pytest.mark.asyncio
    async def test_waiter_served_when_session_returned(self, pool_builder):
        pool = pool_builder(pool_size=1)
        busy = make_session()
        busy.in_use = True
        pool._all_sessions.append(busy)

        # Pool is full and nothing is available -> registers a waiter.
        getter = asyncio.create_task(pool._get_or_create_session())
        await asyncio.sleep(0.01)
        assert len(pool._waiters) == 1

        await pool._return_session(busy)
        served = await asyncio.wait_for(getter, timeout=1.0)

        assert served is busy
        assert served.in_use is True

    @pytest.mark.asyncio
    async def test_get_or_create_times_out_when_exhausted(self, pool_builder):
        pool = pool_builder(pool_size=1, acquire_timeout=0.05)
        busy = make_session()
        busy.in_use = True
        pool._all_sessions.append(busy)

        with pytest.raises(MErrorSocketTimeout):
            await pool._get_or_create_session()

        assert pool._waiters == []  # cleaned up after timeout

    @pytest.mark.asyncio
    async def test_return_unhealthy_session_is_closed(self, pool_builder):
        pool = pool_builder(pool_size=2)
        session = make_session()
        pool._all_sessions.append(session)

        await pool._return_session(session, healthy=False)

        session.http_client.aclose.assert_awaited()
        assert session not in pool._all_sessions
        assert pool.stats["available"] == 0

    @pytest.mark.asyncio
    async def test_return_stale_session_is_discarded(self, pool_builder):
        pool = pool_builder(pool_size=2, max_session_age=60.0)
        session = make_session()
        session.created_at = time.monotonic() - 120
        pool._all_sessions.append(session)

        await pool._return_session(session, healthy=True)

        session.http_client.aclose.assert_awaited()
        assert session not in pool._all_sessions

    @pytest.mark.asyncio
    async def test_acquire_on_closed_pool_raises(self, pool_builder):
        pool = pool_builder(pool_size=1)
        pool._closed = True
        with pytest.raises(RuntimeError, match="closed"):
            async with pool.acquire():
                pass

    @pytest.mark.asyncio
    async def test_warm_creates_and_authenticates(self, pool_builder):
        auth = AsyncMock()
        pool = pool_builder(auth_callback=auth, pool_size=3)

        created = await pool.warm()

        assert created == 3
        assert auth.await_count == 3
        assert pool.stats == {
            "total_sessions": 3,
            "available": 3,
            "in_use": 0,
            "waiting": 0,
            "pool_size": 3,
        }

    @pytest.mark.asyncio
    async def test_warm_is_noop_when_already_full(self, pool_builder):
        pool = pool_builder(pool_size=2)
        await pool.warm()
        assert await pool.warm() == 0

    @pytest.mark.asyncio
    async def test_warm_caps_at_pool_size(self, pool_builder):
        pool = pool_builder(pool_size=2)
        assert await pool.warm(10) == 2

    @pytest.mark.asyncio
    async def test_warm_reports_failed_auth(self, pool_builder):
        auth = AsyncMock(side_effect=RuntimeError("nope"))
        pool = pool_builder(auth_callback=auth, pool_size=2)

        created = await pool.warm()

        assert created == 0
        assert pool._all_sessions == []
        for session in pool_builder.created:
            session.http_client.aclose.assert_awaited()

    @pytest.mark.asyncio
    async def test_close_closes_all_sessions(self, pool_builder):
        pool = pool_builder(pool_size=2)
        await pool.warm()
        sessions = list(pool._all_sessions)

        await pool.close()

        for session in sessions:
            session.http_client.aclose.assert_awaited()
        assert pool._all_sessions == []
        assert pool._pool.empty()
        assert pool._closed is True

    @pytest.mark.asyncio
    async def test_session_ids(self, pool_builder):
        pool = pool_builder(pool_size=3)
        await pool.warm()
        assert sorted(pool.session_ids) == sorted(s.session_id for s in pool._all_sessions)
        assert len(pool.session_ids) == 3

    @pytest.mark.asyncio
    async def test_create_detached_session_adopts_pair_and_is_untracked(
        self, pool_builder
    ):
        auth = AsyncMock()
        pool = pool_builder(auth_callback=auth)

        session = await pool.create_detached_session("COOKIE-VALUE", "oci-session-id")

        session.http_client.cookies.set.assert_called_once_with(
            "JSESSIONID", "COOKIE-VALUE"
        )
        assert session.session_id == "oci-session-id"
        # Detached means: never logged in here, never tracked by the pool.
        auth.assert_not_awaited()
        assert session not in pool._all_sessions
        assert pool._pool.qsize() == 0
