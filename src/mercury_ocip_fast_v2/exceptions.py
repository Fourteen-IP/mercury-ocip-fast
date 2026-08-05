"""
Mercury Fast exceptions
"""

import attrs


@attrs.define(slots=True, frozen=True)
class MError(Exception):
    """Base Exception raised by mercury-ocip-fast.

    attrsibutes:
        message: Why something failed
    """

    message: str = attrs.field(default="An error occurred in unknown project name")

    def __str__(self):
        return f"{self.__class__.__name__}({self.message})"


@attrs.define(slots=True, frozen=True)
class MErrorMissingSessionIdentity(MError):
    """
    Exception raised when an SessionPair is missing a JSESSIONID.
    """

    pass


@attrs.define(slots=True, frozen=True)
class MErrorTransport(MError):
    """A transport-layer failure."""

    pass


@attrs.define(slots=True, frozen=True)
class MErrorSocketMalformedPayload(MErrorTransport):
    """
    Exception raised when the server responds with something undecodeable.
    """

    pass


@attrs.define(slots=True, frozen=True)
class MErrorSocketInitialisation(MErrorTransport):
    """
    Exception raised when the TCP socket fails to initialise.
    """

    pass


@attrs.define(slots=True, frozen=True)
class MErrorSocketTimeout(MErrorTransport):
    """
    Exception raised when the TCP socket to broadworks times out.
    """

    pass


@attrs.define(slots=True, frozen=True)
class MErrorSocketDropped(MErrorTransport):
    """
    Exception raised when Broadworks abruptly closes the socket
    or dropped the connection while data was still being sent.
    """

    pass
