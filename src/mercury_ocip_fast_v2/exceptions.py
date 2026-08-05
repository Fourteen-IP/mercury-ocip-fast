"""
Mercury Fast exceptions
"""

import attr


@attr.s(slots=True, frozen=True)
class MError(Exception):
    """Base Exception raised by mercury-ocip-fast.

    Attributes:
        message: Why something failed
    """

    message: str = attr.ib(default="An error occurred in unknown project name")

    def __str__(self):
        return f"{self.__class__.__name__}({self.message})"


@attr.s(slots=True, frozen=True)
class MErrorMissingSessionIdentity(MError):
    """
    Exception raised when an SessionPair is missing a JSESSIONID.
    """

    pass
