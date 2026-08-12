from collections.abc import Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, ClassVar, Self, TypeVar, get_args, get_origin, get_type_hints

from mercury_ocip_fast.utils.defines import to_snake_case
from mercury_ocip_fast.utils.parser import Parser


class OCIType:
    """
    Base Class For Broadworks Types

    method_table:

    - __init__: Handles dataclass default initialisation of raw objects
    - to_dict: Invokes Parser to_dict_from_class
    - to_xml: Invokes Parser to_xml_from_class
    - from_dict: Invokes Parser to_class_from_dict
    - from_xml: Invokes Parser to_class_from_xml
    """

    namespace = "C"

    def __init__(self, **kwargs):
        annotations = get_type_hints(self.__class__)
        for key, value in kwargs.items():
            if key not in annotations:
                raise ValueError(f"Unknown field: {key}")
            setattr(self, key, value)

        for key in annotations:
            if not hasattr(self, key):
                setattr(self, key, None)

    def get_field_aliases(self) -> dict[str, str]:
        cls = self.__class__
        if not is_dataclass(cls):
            return {}
        return {f.name: f.metadata.get("alias", f.name) for f in fields(cls)}

    def to_dict(self) -> dict[str, Any]:
        return Parser.to_dict_from_class(self)

    def to_xml(self) -> str:
        return Parser.to_xml_from_class(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return Parser.to_class_from_dict(data, cls)

    @classmethod
    def from_xml(cls, xml: str) -> Self:
        return Parser.to_class_from_xml(xml, cls)


class OCICommand(OCIType):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)


T = TypeVar("T")
type Nillable[T] = T


@dataclass
class OCINil:
    pass


class OCIResponse(OCICommand):
    pass


class OCIDataResponse(OCIResponse):
    pass


class SuccessResponse(OCIResponse):
    pass


class OCIRequest[TResponse: OCIResponse](OCICommand):
    """Base type for every OCI request."""

    _response_cls: type[TResponse]


@dataclass
class OCITableRow:
    col: list[str]

    def __init__(self, col):
        self.col = col


@dataclass
class OCITable:
    col_heading: list[str]
    row: list[OCITableRow] = field(default_factory=list)

    def to_dict(self):
        return [
            {
                to_snake_case(self.col_heading[i]): row.col[i]
                for i in range(len(self.col_heading))
            }
            for row in self.row
        ]


class ErrorResponse(OCIResponse):
    error_code: int | None = field(default=None, metadata={"alias": "errorCode"})
    summary: str | None = field(default=None, metadata={"alias": "summary"})
    summary_english: str | None = field(
        default=None, metadata={"alias": "summaryEnglish"}
    )
    detail: str | None = field(default=None, metadata={"alias": "detail"})
