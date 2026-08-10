import logging
from typing import Any, TypeVar, overload

import attrs

from mercury_ocip_fast.commands.base_command import (
    ErrorResponse,
    OCIResponse,
    SuccessResponse,
)
from mercury_ocip_fast.exceptions import MErrorMalformedResponse, MErrorResponse
from mercury_ocip_fast.session.session import SessionAtom
from mercury_ocip_fast.utils.parser import Parser

logger = logging.getLogger(__name__)

R = TypeVar("R", bound=OCIResponse)


@attrs.define(kw_only=True, slots=True)
class Requester:
    """Send OCI payloads over a session and parse the responses.

    A stateless service that delegates transport to a ``SessionAtom`` and
    delegates XML-to-dict parsing to the ``Parser`` utility. The caller
    supplies the expected response type so the result is typed as ``R``.

    Attributes:
        None. This class holds no state; it is a thin orchestration layer
        between a session and the parser.
    """

    @overload
    async def send(
        self, payload: str, response_type: type[R], session: SessionAtom[Any]
    ) -> R: ...

    @overload
    async def send(
        self, payload: list[str], response_type: type[R], session: SessionAtom[Any]
    ) -> list[R]: ...

    async def send(
        self,
        payload: str | list[str],
        response_type: type[R],
        session: SessionAtom[Any],
    ) -> R | list[R]:
        """Send a payload and parse the response.

        Args:
            payload: The OCI command XML string to send.
            response_type: The expected response class, used to parse each
                command in the reply.
            session: The session atom that carries the payload to the server
                and returns the reply string.

        Returns:
            A single parsed response, or a list of parsed responses when the
            reply contains more than one command.

        Raises:
            MErrorMalformedResponse: If the reply cannot be parsed or has an
                unexpected structure.
            MErrorResponse: If the server returns an ``ErrorResponse``.
        """
        logger.debug("Sending OCI payload to session %s", session.session_id)
        response = await session.send(payload)
        logger.debug(
            "Received %d bytes from session %s",
            len(response),
            session.session_id,
        )
        return self.parse_response(response, response_type)

    def parse_response(self, payload: str, response_type: type[R]) -> R | list[R]:
        """Parse an OCI reply string into typed response object(s).

        Args:
            payload: The raw reply XML string received from the server.
            response_type: The expected response class for each command.

        Returns:
            A single response, or a list of responses when the reply carries
            more than one command.

        Raises:
            MErrorMalformedResponse: If the reply is empty or cannot be parsed.
            MErrorResponse: If any command in the reply is an ``ErrorResponse``.
        """
        response_dict = Parser.to_dict_from_xml(payload)
        command_data = response_dict.get("command")

        if isinstance(command_data, list):
            logger.debug("Parsing %d commands from batch response", len(command_data))
            return [
                self._parse_single_response(cmd, response_type) for cmd in command_data
            ]

        if isinstance(command_data, dict):
            return self._parse_single_response(command_data, response_type)

        if not command_data:
            logger.warning("Malformed reply: no command data found in payload")
            raise MErrorMalformedResponse(payload)

        logger.warning(
            "Malformed reply: command data is %s, expected list or dict",
            type(command_data).__name__,
        )
        raise MErrorMalformedResponse(payload)

    def _parse_single_response(self, payload: dict, response_type: type[R]) -> R:
        """Parse a single command dict into a response object.

        Args:
            payload: A single command dict as produced by the parser. It is
                expected to contain an ``attributes`` key with an
                ``xsi:type`` entry identifying the response type.
            response_type: The expected response class to instantiate.

        Returns:
            The parsed response object of type ``R``.

        Raises:
            MErrorMalformedResponse: If the ``xsi:type`` attribute is missing
                or cannot be resolved.
            MErrorResponse: If the command is an ``ErrorResponse``.
        """
        type_name: str | None = payload.get("attributes", {}).get(
            "{http://www.w3.org/2001/XMLSchema-instance}type"
        )

        if not type_name:
            raise MErrorMalformedResponse("Failed to parse response object")

        type_name = type_name.split(":", 1)[-1]

        if type_name == "ErrorResponse":
            summary = ErrorResponse.from_dict(payload).summaryEnglish
            logger.warning("Received ErrorResponse from server: %s", summary)
            raise MErrorResponse(summary)

        return response_type.from_dict(payload)
