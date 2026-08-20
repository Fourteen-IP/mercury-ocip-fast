"""Schema-driven OCI parser."""

from __future__ import annotations

import types
from dataclasses import MISSING, dataclass, fields, is_dataclass
from enum import Enum, auto
from functools import cache
from typing import Any, ClassVar, Union, cast, get_args, get_origin, get_type_hints

import xmltodict

from mercury_ocip_fast.utils.defines import snake_to_camel, to_snake_case
from mercury_ocip_fast.utils.oci_types import OCINil, OCITable, OCITableRow


class FieldKind(Enum):
    SCALAR = auto()
    OBJECT = auto()
    LIST = auto()
    TABLE = auto()


@dataclass(frozen=True)
class FieldSpec:
    """Hold the resolved metadata for one dataclass field."""

    name: str
    alias: str
    kind: FieldKind
    subtype: type | None


class Parser:
    """Translate OCI objects between class, dict and XML forms."""

    _CONVERTERS: ClassVar[dict[type, Any]] = {
        bool: lambda v: v.lower() == "true",
        int: int,
        float: float,
    }

    @staticmethod
    def to_xml_from_class(obj: object) -> str:
        """Convert an object to an XML command string."""
        # "@" keys become XML attributes on the <command> root element
        root = {
            "@xmlns": "",
            "@xsi:type": type(obj).__name__,
            **Parser._encode(obj),
        }
        return xmltodict.unparse(
            {"command": root}, full_document=False, short_empty_elements=False
        )

    @staticmethod
    def to_xml_from_dict[T](data: dict[str, Any], cls: type[T]) -> str:
        """Convert a dict to an XML command string."""
        return Parser.to_xml_from_class(Parser._decode(data, cls))

    @staticmethod
    def to_dict_from_class(
        obj: object, wrap_in_class_name: bool = False
    ) -> dict[str, Any]:
        """Convert an object to a dict."""
        result = Parser._encode(obj)
        return {type(obj).__name__: result} if wrap_in_class_name else result

    @staticmethod
    def to_dict_from_xml(xml: str) -> dict[str, Any]:
        """Parse an XML string into a dict."""
        if not isinstance(xml, str):
            return {}
        parsed = xmltodict.parse(xml)
        if not parsed:
            return {}
        root_key = next(iter(parsed))
        return cast(dict[str, Any], Parser._clean(root_key, parsed[root_key]))

    @staticmethod
    def to_class_from_dict[T](data: dict[str, Any], cls: type[T]) -> T:
        """Convert a dict to an instance of cls."""
        return Parser._decode(data, cls)

    @staticmethod
    def to_class_from_xml[T](xml: str, cls: type[T]) -> T:
        """Parse an XML string and convert it to an instance of cls."""
        return Parser._decode(Parser.to_dict_from_xml(xml), cls)

    @staticmethod
    @cache
    def _schema(target: type) -> tuple[FieldSpec, ...]:
        """Build the field schema for a class. Cache the result."""
        field_map = {f.name: f for f in fields(target)} if is_dataclass(target) else {}
        specs = []

        for name, hint in get_type_hints(target).items():
            if name.startswith("_"):
                continue

            f = field_map.get(name)
            alias = (
                f.metadata["alias"]
                if f and "alias" in f.metadata
                else snake_to_camel(name)
            )
            inner = Parser._unwrap(hint)

            if isinstance(inner, type) and issubclass(inner, OCITable):
                kind, subtype = FieldKind.TABLE, None
            elif get_origin(inner) is list:
                args = get_args(inner)
                kind, subtype = FieldKind.LIST, args[0] if args else None
            elif isinstance(inner, type) and is_dataclass(inner):
                kind, subtype = FieldKind.OBJECT, inner
            else:
                kind = FieldKind.SCALAR
                subtype = inner if isinstance(inner, type) else None

            specs.append(FieldSpec(name, alias, kind, subtype))

        return tuple(specs)

    @staticmethod
    def _unwrap(hint: Any) -> Any:
        """Remove Optional, Union and Nillable wrappers from a type hint."""
        while True:
            origin = get_origin(hint)
            args = get_args(hint)

            # Optional[X] and X | None have different origins at runtime
            if origin is Union or isinstance(hint, types.UnionType):
                non_none = [a for a in args if a is not type(None)]
                if len(non_none) != 1:
                    return hint
                hint = non_none[0]
            elif origin is list or origin is None or not args:
                return hint
            else:
                # Nillable[T] is a runtime alias
                hint = args[0]

    @staticmethod
    def _encode(obj: object, declared: type | None = None) -> dict[str, Any]:
        """Convert an object to a wire dict."""
        cls = type(obj)
        result: dict[str, Any] = {}

        if declared is not None and declared.__name__ != cls.__name__:
            result["@xsi:type"] = cls.__name__

        for spec in Parser._schema(cls):
            value = getattr(obj, spec.name, None)
            if value is None:
                continue
            if isinstance(value, OCINil):
                result[spec.alias] = {"@C:nil": "true"}
            elif isinstance(value, bool):
                result[spec.alias] = str(value).lower()
            elif isinstance(value, OCITable):
                result[spec.alias] = {
                    "colHeading": value.col_heading,
                    "row": [{"col": row.col} for row in value.row],
                }
            elif spec.kind is FieldKind.OBJECT:
                result[spec.alias] = Parser._encode(value, declared=spec.subtype)
            elif spec.kind is FieldKind.LIST and value:
                result[spec.alias] = Parser._encode_list(value, spec)
            elif spec.kind is FieldKind.SCALAR:
                result[spec.alias] = value

        return result

    @staticmethod
    def _encode_list(items: list[Any], spec: FieldSpec) -> list[Any] | dict[str, Any]:
        """Convert a list field. Emit table format for table-shaped dict lists."""
        first = items[0]
        if (
            isinstance(first, dict)
            and spec.alias.endswith("Table")
            and all(isinstance(i, dict) and i.keys() == first.keys() for i in items)
        ):
            headings = list(first)
            return {
                "colHeading": headings,
                "row": [{"col": [str(i.get(k, "")) for k in headings]} for i in items],
            }

        result = []
        for item in items:
            if isinstance(item, bool):
                result.append(str(item).lower())
            elif is_dataclass(item) and not isinstance(item, type):
                result.append(Parser._encode(item, declared=spec.subtype))
            else:
                result.append(item)
        return result

    @staticmethod
    def _decode[T](data: dict[str, Any], cls: type[T]) -> T:
        """Convert a wire dict to an instance of cls."""
        if not isinstance(data, dict):
            raise TypeError(
                f"Expected dict for {cls.__name__}, got {type(data).__name__}"
            )

        source = data.get(cls.__name__, data.get("command", data))
        if not isinstance(source, dict):
            raise TypeError(
                f"Expected dict for {cls.__name__}, got {type(source).__name__}"
            )

        cls = Parser._concrete_type(cls, source)

        source = {to_snake_case(k): v for k, v in source.items()}
        init_args: dict[str, Any] = {}

        for spec in Parser._schema(cls):
            if spec.name not in source:
                continue
            value = source[spec.name]

            if spec.kind is FieldKind.OBJECT and isinstance(value, dict):
                value = Parser._decode(value, cast(type, spec.subtype))
            elif spec.kind is FieldKind.TABLE:
                value = Parser._decode_table(value)
            elif spec.kind is FieldKind.LIST:
                # xmltodict yields a bare value for a single element
                items = value if isinstance(value, list) else [value]
                value = [
                    Parser._decode(i, spec.subtype)
                    if isinstance(i, dict) and spec.subtype
                    else Parser._coerce(i, spec.subtype)
                    for i in items
                ]
            else:
                value = Parser._coerce(value, spec.subtype)
            init_args[spec.name] = value

        # Missing required fields become None so partial responses construct
        if is_dataclass(cls):
            for f in fields(cls):
                if (
                    f.init
                    and f.name not in init_args
                    and f.default is MISSING
                    and f.default_factory is MISSING
                ):
                    init_args[f.name] = None

        return cls(**init_args)

    @staticmethod
    def _concrete_type[T](declared: type[T], source: dict[str, Any]) -> type[T]:
        """Return the xsi:type subtype named in source, or declared itself.

        A field may declare an abstract base, for example ``DepartmentKey``,
        while the reply carries a concrete subtype such as
        ``EnterpriseDepartmentKey`` marked with ``xsi:type``.
        """
        attributes = source.get("attributes")
        if not isinstance(attributes, dict):
            return declared

        xsi_type = attributes.get("type") or attributes.get(
            "{http://www.w3.org/2001/XMLSchema-instance}type"
        )
        if not isinstance(xsi_type, str):
            return declared

        name = xsi_type.split(":", 1)[-1]
        stack = list(declared.__subclasses__())
        while stack:
            sub = stack.pop()
            if sub.__name__ == name:
                return sub
            stack.extend(sub.__subclasses__())

        return declared

    @staticmethod
    def _coerce(value: Any, target: type | None) -> Any:
        """Convert a wire string to the scalar type of the field."""
        convert = Parser._CONVERTERS.get(target) if target is not None else None
        if convert is None or not isinstance(value, str):
            return value
        try:
            return convert(value)
        except ValueError:
            # Keep unparseable values, for example an empty tag, as-is
            return value

    @staticmethod
    def _decode_table(value: Any) -> Any:
        """Convert a colHeading/row dict to an OCITable."""
        if not isinstance(value, dict) or "colHeading" not in value:
            return value

        def as_list(v: Any) -> list[Any]:
            return v if isinstance(v, list) else [v]

        return OCITable(
            col_heading=as_list(value["colHeading"]),
            row=[
                OCITableRow(col=as_list(r.get("col", [])))
                for r in as_list(value.get("row", []))
            ],
        )

    @staticmethod
    def _clean(key: str, value: Any) -> Any:
        """Normalize one node of xmltodict output."""
        if (
            "Table" in key
            and isinstance(value, dict)
            and "colHeading" in value
            and "row" in value
        ):
            return Parser._decode_table(value)

        if not isinstance(value, dict):
            return "" if value is None else value

        if "#text" in value:
            return value["#text"]

        cleaned: dict[str, Any] = {}
        attributes: dict[str, Any] = {}

        for k, v in value.items():
            if not k.startswith("@"):
                cleaned[k] = (
                    [Parser._clean(k, i) for i in v]
                    if isinstance(v, list)
                    else Parser._clean(k, v)
                )
                continue

            name = k[1:]
            attributes[name] = v
            if ":" in name:
                prefix, local = name.split(":", 1)
                attributes[local] = v
                # Consumers match attributes in W3C Clark notation
                if prefix in ("xsi", "C"):
                    key_ns = "{http://www.w3.org/2001/XMLSchema-instance}"
                    attributes[f"{key_ns}{local}"] = v

        if attributes:
            cleaned["attributes"] = attributes
        return cleaned
