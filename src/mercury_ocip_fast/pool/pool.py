import attrs


@attrs.define(slots=True, frozen=True)
class SessionPoolSettings:
    """Config for the Session Pool."""

    max_size: int = 5
    acquire_timeout: float = 10.0
    wait_timeout: float = 10.0
