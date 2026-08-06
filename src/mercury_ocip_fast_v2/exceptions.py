"""
Mercury Fast exceptions
"""

import attrs


@attrs.define(slots=True, frozen=True)
class MError(Exception):
    """Base Exception raised by mercury-ocip-fast.

    Attributes:
        message: Why something failed
    """

    message: str = attrs.field(default="An error occurred in mercury-ocip-fast")

    def __str__(self):
        return f"{self.__class__.__name__}({self.message})"


@attrs.define(slots=True, frozen=True)
class MErrorMissingSessionIdentity(MError):
    """
    Exception raised when an SessionPair is missing a JSESSIONID.
    """


@attrs.define(slots=True, frozen=True)
class MErrorLogin(MError):
    """
    A connection failed to log in.
    """


@attrs.define(slots=True, frozen=True)
class MErrorTransport(MError):
    """A transport-layer failure."""


@attrs.define(slots=True, frozen=True)
class MErrorMalformedResponse(MErrorTransport):
    """The server responded with something undecodeable."""


@attrs.define(slots=True, frozen=True)
class MErrorHttpInitialisation(MErrorTransport):
    """The HTTP connection to BroadWorks could not be established."""


@attrs.define(slots=True, frozen=True)
class MErrorHttpTimeout(MErrorTransport):
    """The HTTP request to BroadWorks timed out."""


@attrs.define(slots=True, frozen=True)
class MErrorHttpDropped(MErrorTransport):
    """The HTTP connection dropped mid-request."""


@attrs.define(slots=True, frozen=True)
class MErrorHttpStatus(MErrorTransport):
    """BroadWorks returned a non-2xx HTTP status."""

    status: int = attrs.field(kw_only=True)


@attrs.define(slots=True, frozen=True)
class MErrorSocketInitialisation(MErrorTransport):
    """The TCP socket failed to initialise."""


@attrs.define(slots=True, frozen=True)
class MErrorSocketTimeout(MErrorTransport):
    """The TCP socket's connection to Broadworks timed out."""


@attrs.define(slots=True, frozen=True)
class MErrorSocketDropped(MErrorTransport):
    """
    Broadworks abruptly closed the TCP socket or dropped the
    connection while data was still being sent.
    """
