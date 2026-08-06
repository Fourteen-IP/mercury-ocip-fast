import attrs


@attrs.define(slots=True, frozen=True)
class SessionPoolSettings:
    """Config for the Session Pool."""

    max_size: int = 5
