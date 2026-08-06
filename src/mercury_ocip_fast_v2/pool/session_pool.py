import asyncio
from collections.abc import Awaitable, Callable

import attrs

from mercury_ocip_fast_v2.pool.pool import SessionPoolSettings
from mercury_ocip_fast_v2.session.session import (
    SessionAtom,
    SessionPair,
)


@attrs.define(slots=True)
class SessionPool[A: SessionAtom]:
    _create_fn: Callable[[], Awaitable[A]]
    _auth_fn: Callable[[A], Awaitable[SessionPair]] | None = None
    pool_settings: SessionPoolSettings = attrs.field(default=SessionPoolSettings())
    _idle: asyncio.LifoQueue[A] = attrs.field(factory=asyncio.LifoQueue)
    _semaphore: asyncio.Semaphore = attrs.field(init=False)
    _all: list[A] = attrs.field(factory=list)
    _closed: bool = attrs.field(default=False)

    def __attrs_post_init__(self) -> None:
        self._semaphore = asyncio.Semaphore(self.pool_settings.max_size)

    async def aquire(self) -> A: ...
