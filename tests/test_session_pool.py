"""Tests for the ``SessionPool``: borrow, return, and discard session atoms."""

import pytest

from mercury_ocip_fast.exceptions import MErrorPoolClosed, MErrorPoolExhausted
from mercury_ocip_fast.pool.pool import SessionPoolSettings
from mercury_ocip_fast.pool.session_pool import SessionPool

from conftest import FakeAtom


def make_pool(**settings_kwargs) -> tuple[SessionPool, list[FakeAtom]]:
    """Build a pool that hands out fresh fake atoms and records each one."""
    created: list[FakeAtom] = []

    async def factory() -> FakeAtom:
        atom = FakeAtom(session_id=f"atom-{len(created)}")
        created.append(atom)
        return atom

    settings = SessionPoolSettings(**settings_kwargs)
    return SessionPool(default_factory=factory, pool_settings=settings), created


class TestAcquireRelease:
    async def test_acquire_creates_then_reuses(self):
        pool, created = make_pool(max_size=2)

        atom = await pool.acquire()
        assert len(created) == 1
        await pool.release(atom)

        # The idle atom comes back on the next acquire; no new atom is made.
        again = await pool.acquire()
        assert again is atom
        assert len(created) == 1
        await pool.release(again)
        await pool.close()

    async def test_session_context_returns_healthy_atom(self):
        pool, created = make_pool(max_size=1)
        async with pool.session() as atom:
            assert atom.is_alive()
        # The atom went back to the idle queue, not closed.
        assert created[0].closed is False
        await pool.close()

    async def test_session_context_discards_on_error(self):
        pool, created = make_pool(max_size=1)
        with pytest.raises(RuntimeError):
            async with pool.session() as atom:
                raise RuntimeError("boom")
        # A failed block marks the atom unhealthy, so the pool closes it.
        assert created[0].closed is True
        await pool.close()


class TestHealthChecks:
    async def test_dead_idle_atom_is_discarded(self):
        pool, created = make_pool(max_size=2)
        atom = await pool.acquire()
        await pool.release(atom)

        # The idle atom is now dead. The next acquire drops it and makes a new one.
        atom.alive = False
        fresh = await pool.acquire()
        assert fresh is not atom
        assert atom.closed is True
        await pool.release(fresh)
        await pool.close()

    async def test_stale_idle_atom_is_discarded(self):
        pool, created = make_pool(max_size=2)
        atom = await pool.acquire()
        await pool.release(atom)

        atom.stale = True
        fresh = await pool.acquire()
        assert fresh is not atom
        await pool.release(fresh)
        await pool.close()

    async def test_unhealthy_release_closes_atom(self):
        pool, created = make_pool(max_size=1)
        atom = await pool.acquire()
        await pool.release(atom, healthy=False)
        assert atom.closed is True
        await pool.close()


class TestLimits:
    async def test_exhaustion_raises(self):
        pool, _ = make_pool(max_size=1, acquire_timeout=0.05)
        first = await pool.acquire()
        with pytest.raises(MErrorPoolExhausted):
            await pool.acquire()
        await pool.release(first)
        await pool.close()

    async def test_acquire_on_closed_pool_raises(self):
        pool, _ = make_pool(max_size=1)
        await pool.close()
        with pytest.raises(MErrorPoolClosed):
            await pool.acquire()


class TestClose:
    async def test_close_shuts_idle_atoms(self):
        pool, created = make_pool(max_size=2)
        a = await pool.acquire()
        b = await pool.acquire()
        await pool.release(a)
        await pool.release(b)
        await pool.close()
        assert all(atom.closed for atom in created)

    async def test_close_is_idempotent(self):
        pool, _ = make_pool(max_size=1)
        await pool.close()
        await pool.close()  # A second close is safe.

    async def test_in_use_count(self):
        pool, _ = make_pool(max_size=2)
        a = await pool.acquire()
        assert pool._in_use == 1
        await pool.release(a)
        assert pool._in_use == 0
        await pool.close()
