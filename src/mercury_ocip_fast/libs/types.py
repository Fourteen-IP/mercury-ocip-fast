from typing import Union, TypeVar
from mercury_ocip_fast.commands.base_command import (
    ErrorResponse,
    SuccessResponse,
    OCIDataResponse,
    OCICommand,
)
from mercury_ocip_fast.libs.basic_types import (
    RequestResult,
    ConnectResult,
    DisconnectResult,
    XMLDictResult,
)

__all__ = [
    "RequestResult",
    "ConnectResult",
    "DisconnectResult",
    "XMLDictResult",
    "CommandResponse",
    "CommandInput",
    "CommandResult",
]

T = TypeVar("T", bound=OCIDataResponse)

type CommandResponse[T] = Union[ErrorResponse, SuccessResponse, T]

# What client.command() accepts and returns
type CommandInput = OCICommand
type CommandResult[T] = Union[CommandResponse[T], None]
