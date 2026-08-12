import logging
from types import UnionType
from typing import Any, TypeVar, cast, get_args, overload

import attrs

from mercury_ocip_fast.commands.base_command import (
    ErrorResponse,
    OCIResponse,
)
from mercury_ocip_fast.exceptions import MErrorMalformedResponse
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
        self,
        payload: list[str],
        response_type: type[R] | list[type[R]],
        session: SessionAtom[Any],
    ) -> list[R]: ...

    async def send(
        self,
        payload: str | list[str],
        response_type: type[R] | list[type[R]],
        session: SessionAtom[Any],
    ) -> R | list[R]:
        """Send a payload and parse the response.

        Args:
            payload: The OCI command XML string to send.
            response_type: The expected response class, used to parse each
                command in the reply. For a batch, pass a list of classes
                aligned with ``payload`` order to parse each command as its
                own type; a single class is applied to every command.
            session: The session atom that carries the payload to the server
                and returns the reply string.

        Returns:
            A single parsed response for a single payload, or a list of
            parsed responses for a list payload. An ``ErrorResponse`` from
            the server is returned like any other response, not raised.

        Raises:
            MErrorMalformedResponse: If the reply cannot be parsed or has an
                unexpected structure.
        """
        logger.debug("Sending OCI payload to session %s", session.session_id)
        response = await session.send(payload)
        logger.debug(
            "Received %d bytes from session %s",
            len(response),
            session.session_id,
        )
        parsed = self.parse_response(response, response_type)

        if isinstance(payload, list) and not isinstance(parsed, list):
            return [parsed]

        return parsed

    def parse_response(
        self, payload: str, response_type: type[R] | list[type[R]]
    ) -> R | list[R]:
        """Parse an OCI reply string into typed response object(s).

        Args:
            payload: The raw reply XML string received from the server.
            response_type: The expected response class for each command, or a
                list of classes aligned with the commands in the reply.

        Returns:
            A single response, or a list of responses when the reply carries
            more than one command. An ``ErrorResponse`` is returned in place,
            like any other response.

        Raises:
            MErrorMalformedResponse: If the reply is empty, cannot be parsed,
                or carries a different number of commands than the batch sent.
        """
        response_dict = Parser.to_dict_from_xml(payload)
        command_data = response_dict.get("command")

        if isinstance(command_data, list):
            logger.debug("Parsing %d commands from batch response", len(command_data))
            types = self._align_types(response_type, len(command_data), payload)
            return [
                self._parse_single_response(cmd, t)
                for cmd, t in zip(command_data, types)
            ]

        if isinstance(command_data, dict):
            single = (
                response_type[0] if isinstance(response_type, list) else response_type
            )
            return self._parse_single_response(command_data, single)

        if not command_data:
            logger.warning("Malformed reply: no command data found in payload")
            raise MErrorMalformedResponse(payload)

        logger.warning(
            "Malformed reply: command data is %s, expected list or dict",
            type(command_data).__name__,
        )
        raise MErrorMalformedResponse(payload)

    def _align_types(
        self, response_type: type[R] | list[type[R]], count: int, payload: str
    ) -> list[type[R]]:
        """Line up one response class per command in a batch reply.

        Raises:
            MErrorMalformedResponse: If a list is given whose length differs
                from the number of commands in the reply.
        """
        if not isinstance(response_type, list):
            return [response_type] * count

        if len(response_type) != count:
            logger.warning(
                "Batch reply has %d commands but %d response types were given",
                count,
                len(response_type),
            )
            raise MErrorMalformedResponse(payload)

        return response_type

    @staticmethod
    def _concrete(response_type: type[R]) -> type[R]:
        """Resolve the concrete response class to instantiate."""
        if isinstance(response_type, UnionType):
            for member in get_args(response_type):
                if member is not ErrorResponse:
                    return cast(type[R], member)
        return response_type

    def _parse_single_response(self, payload: dict, response_type: type[R]) -> R:
        """Parse a single command dict into a response object.

        Args:
            payload: A single command dict as produced by the parser. It is
                expected to contain an ``attributes`` key with an
                ``xsi:type`` entry identifying the response type.
            response_type: The expected response class, or a
                ``Response | ErrorResponse`` union, to instantiate.

        Returns:
            The parsed response object of type ``R``. A server-side
            ``ErrorResponse`` is returned like any other response.

        Raises:
            MErrorMalformedResponse: If the ``xsi:type`` attribute is missing
                or cannot be resolved.
        """
        type_name: str | None = payload.get("attributes", {}).get(
            "{http://www.w3.org/2001/XMLSchema-instance}type"
        )

        if not type_name:
            raise MErrorMalformedResponse("Failed to parse response object")

        type_name = type_name.split(":", 1)[-1]

        if type_name == "ErrorResponse":
            response = ErrorResponse.from_dict(payload)
            logger.warning(
                "Received ErrorResponse from server: %s",
                response.summary or "No Summary",
            )
            return cast(R, response)

        return self._concrete(response_type).from_dict(payload)
