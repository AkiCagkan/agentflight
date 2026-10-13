"""Schemas: `Schema` classes, Pydantic models, JSON schema generation and validation.

    class Summary(Schema):                       # or pydantic.BaseModel
        title: str
        bullets: list[str]
        score: int = field("1-10 quality score", min=1, max=10)

This module builds JSON schemas from these classes (and from types such as list[...], Literal[...],
Optional[...]) and validates the data that comes back from the model, turning it into real objects. When
Pydantic is installed, `BaseModel` subclasses can also be used as return types, tool parameters and field types.
"""

from __future__ import annotations

import copy
import inspect
import json
import re
import sys
import types
import typing
from typing import Any, Literal, Union

from .errors import SchemaError

MISSING = object()

RESULT_KEY = "result"
"""Key that wraps non-object return types in the output schema."""


class FieldInfo:
    """Extra information about a schema field (description, limits, default)."""

    __slots__ = ("description", "default", "min", "max", "pattern", "example")

    def __init__(self, description="", *, default=MISSING, min=None, max=None, pattern=None,  # noqa: A002
                 example=None):
        self.description = description
        self.default = default
        self.min = min
        self.max = max
        self.pattern = pattern
        self.example = example

    def __repr__(self):
        return f"field({self.description!r})"


def field(description: str = "", *, default=MISSING, min=None, max=None, pattern=None,  # noqa: A002
          example=None) -> Any:
    """Adds a description and limits to a schema field.

    min / max: value for numbers, length for strings, number of items for lists.

        score: int = field("1-10 quality score", min=1, max=10)
    """
    return FieldInfo(description, default=default, min=min, max=max, pattern=pattern, example=example)


class _FieldSpec:
    __slots__ = ("name", "type", "meta", "default")

    def __init__(self, name, tp, meta, default):
        self.name = name
        self.type = tp
        self.meta = meta
        self.default = default

    @property
    def required(self):
        return self.default is MISSING


class Schema:
    """Base class for schemas: typed fields the model must fill in."""

    __agentflight_schema__ = True

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        cls._field_cache = None

    @classmethod
    def _fields(cls) -> dict[str, _FieldSpec]:
        if cls.__dict__.get("_field_cache") is not None:
            return cls._field_cache
        fields: dict[str, _FieldSpec] = {}
        for base in reversed(cls.__mro__):
            if base is Schema or not (isinstance(base, type) and issubclass(base, Schema)):
                continue
            annotations = inspect.get_annotations(base)
            if not annotations:
                continue
            module = sys.modules.get(base.__module__)
            globalns = dict(vars(module)) if module else {}
            localns = dict(vars(base))
            for name, annotation in annotations.items():
                if name.startswith("_"):
                    continue
                tp = _resolve(annotation, globalns, localns)
                if typing.get_origin(tp) is typing.ClassVar:
                    continue
                value = base.__dict__.get(name, MISSING)
                if isinstance(value, FieldInfo):
                    meta, default = value, value.default
                else:
                    meta, default = None, value
                if default is MISSING and _is_optional(tp):
                    default = None
                fields[name] = _FieldSpec(name, tp, meta, default)
        cls._field_cache = fields
        return fields

    def __init__(self, **data):
        errors: list[str] = []
        converted = _validate_object(type(self), data, "", errors)
        if errors:
            raise SchemaError(errors)
        for name, value in converted.items():
            object.__setattr__(self, name, value)

    @classmethod
    def validate(cls, data):
        """Validates a dict (or JSON text) and returns an instance of this schema."""
        if isinstance(data, str):
            data = json.loads(data)
        if isinstance(data, cls):
            return data
        errors: list[str] = []
        result = convert(cls, data, "", errors)
        if errors:
            raise SchemaError(errors)
        return result

    @classmethod
    def json_schema(cls) -> dict:
        """JSON schema of this class."""
        return json_schema(cls)  # the module-level function

    def to_dict(self) -> dict:
        return {name: _plain(getattr(self, name)) for name in type(self)._fields()}

    def to_json(self, indent=None) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def __eq__(self, other):
        return type(self) is type(other) and self.to_dict() == other.to_dict()

    def __hash__(self):
        return hash(self.to_json())

    def __repr__(self):
        parts = ", ".join(f"{name}={getattr(self, name)!r}" for name in type(self)._fields())
        return f"{type(self).__name__}({parts})"


# ---------------------------------------------------------------------------
# Pydantic (optional)

def is_pydantic_model(t) -> bool:
    """Is `t` a Pydantic BaseModel subclass? (always False when pydantic is not installed)"""
    if not isinstance(t, type):
        return False
    module = sys.modules.get("pydantic")
    if module is None:
        if not any(k.__module__.startswith("pydantic") for k in getattr(t, "__mro__", ())):
            return False
        try:
            import pydantic as module  # noqa: F811
        except ImportError:
            return False
    try:
        return issubclass(t, module.BaseModel) and t is not module.BaseModel
    except TypeError:
        return False


def is_object_type(t) -> bool:
    """Is `t` an object type with fields (a Schema class or a Pydantic model)?"""
    return is_schema_class(t) or is_pydantic_model(t)


def _inline_refs(node, defs, stack=()):
    """Replaces the $refs of a Pydantic schema with their definitions (most providers do not support $defs)."""
    if isinstance(node, list):
        return [_inline_refs(n, defs, stack) for n in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        name = node["$ref"].rsplit("/", 1)[-1]
        if name in stack or name not in defs:  # recursive model: cannot be inlined, use a free-form object
            return {"type": "object"}
        inlined = _inline_refs(defs[name], defs, (*stack, name))
        extra = {k: v for k, v in node.items() if k != "$ref"}
        return {**inlined, **_inline_refs(extra, defs, stack)}
    return {k: _inline_refs(v, defs, stack) for k, v in node.items() if k not in ("$defs", "definitions")}


_LIMIT_KEYS = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength",
               "pattern", "minItems", "maxItems", "multipleOf", "format")


def _simplify(s, strict: bool):
    """Drops titles; in strict mode moves limits into the description. Objects get additionalProperties: false."""
    if isinstance(s, list):
        return [_simplify(x, strict) for x in s]
    if not isinstance(s, dict):
        return s
    out = {}
    for k, v in s.items():
        if k == "title":
            continue
        if k in ("properties", "$defs") and isinstance(v, dict):
            out[k] = {name: _simplify(sub, strict) for name, sub in v.items()}
        else:
            out[k] = _simplify(v, strict)
    if strict:
        limits = {k: out.pop(k) for k in _LIMIT_KEYS if k in out}
        out.pop("default", None)
        if limits:
            extra = ", ".join(_limit_text(k, v) for k, v in limits.items())
            out["description"] = f"{out.get('description', '')} ({extra})".strip()
    if out.get("type") == "object" and "properties" in out:
        out.setdefault("additionalProperties", False)
    return out


def _pydantic_schema(t, strict: bool) -> dict:
    raw = t.model_json_schema()
    return _simplify(_inline_refs(raw, raw.get("$defs", {})), strict)


def _pydantic_validate(t, value, path: str, errors: list[str]):
    from pydantic import ValidationError

    if isinstance(value, t):
        return value
    try:
        return t.model_validate(value)
    except ValidationError as e:
        for err in e.errors():
            where = path
            for part in err.get("loc", ()):
                where = _path(where, part)
            errors.append(f"{where or t.__name__}: {err.get('msg', 'invalid')}")
        return None


# ---------------------------------------------------------------------------
# helpers

def _resolve(annotation, globalns, localns):
    if isinstance(annotation, str):
        try:
            return eval(annotation, {**_TYPE_NAMES, **globalns}, localns)  # noqa: S307 - type expression
        except Exception as e:  # pragma: no cover - the checker catches this earlier
            raise SchemaError(f"could not resolve type: {annotation} ({e})") from e
    return annotation


_TYPE_NAMES = {
    "Literal": Literal, "Optional": typing.Optional, "Union": Union, "Any": Any,
    "List": list, "Dict": dict,
}


def _is_optional(tp) -> bool:
    origin = typing.get_origin(tp)
    if origin in (Union, types.UnionType):
        return type(None) in typing.get_args(tp)
    return False


def is_schema_class(t) -> bool:
    return isinstance(t, type) and issubclass(t, Schema) and t is not Schema


def type_name(t) -> str:
    if t is type(None) or t is None:
        return "None"
    if t is Any:
        return "Any"
    origin = typing.get_origin(t)
    if origin is Literal:
        return " | ".join(repr(a) for a in typing.get_args(t))
    if origin in (Union, types.UnionType):
        return " | ".join(type_name(a) for a in typing.get_args(t))
    if origin is not None:
        names = ", ".join(type_name(a) for a in typing.get_args(t))
        return f"{getattr(origin, '__name__', str(origin))}[{names}]"
    return getattr(t, "__name__", str(t))


def _is_pydantic_instance(value) -> bool:
    return hasattr(value, "model_dump") and is_pydantic_model(type(value))


def _plain(value):
    if isinstance(value, Schema):
        return value.to_dict()
    if _is_pydantic_instance(value):
        return value.model_dump(mode="json")
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def to_jsonable(value, truncate: int | None = None):
    """Turns any value into something that can be written as JSON."""
    if isinstance(value, Schema):
        return value.to_dict()
    if _is_pydantic_instance(value):
        return to_jsonable(value.model_dump(mode="json"), truncate)
    if isinstance(value, (str, int, float, bool)) or value is None:
        if truncate and isinstance(value, str) and len(value) > truncate:
            return value[:truncate] + f"… (+{len(value) - truncate})"
        return value
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v, truncate) for v in value]
    if isinstance(value, dict):
        return {str(k): to_jsonable(v, truncate) for k, v in value.items()}
    text = repr(value)
    if truncate and len(text) > truncate:
        text = text[:truncate] + "…"
    return text


# ---------------------------------------------------------------------------
# JSON schema

def json_schema(t, strict: bool = False, meta: FieldInfo | None = None) -> dict:
    """Builds a JSON schema from a Python / Schema / Pydantic type.

    strict=True: limits that the providers' native schema modes do not support (minimum, maxLength,
    pattern...) are removed from the schema and written into the description instead. AgentFlight still
    validates them.
    """
    s = _schema(t, strict)
    if meta is not None:
        s = dict(s)
        limits = _limit_keywords(t, meta)
        description = meta.description or ""
        if strict and limits:
            extra = ", ".join(_limit_text(k, v) for k, v in limits.items())
            description = f"{description} ({extra})".strip()
        elif limits:
            s.update(limits)
        if description:
            s["description"] = description
    return s


def _limit_keywords(t, meta: FieldInfo) -> dict:
    base = _base_type(t)
    limits: dict = {}
    if base in (int, float):
        if meta.min is not None:
            limits["minimum"] = meta.min
        if meta.max is not None:
            limits["maximum"] = meta.max
    elif base is str:
        if meta.min is not None:
            limits["minLength"] = meta.min
        if meta.max is not None:
            limits["maxLength"] = meta.max
        if meta.pattern:
            limits["pattern"] = meta.pattern
    elif base is list:
        if meta.min is not None:
            limits["minItems"] = meta.min
        if meta.max is not None:
            limits["maxItems"] = meta.max
    return limits


def _limit_text(key, value):
    texts = {
        "minimum": f"at least {value}", "maximum": f"at most {value}",
        "exclusiveMinimum": f"greater than {value}", "exclusiveMaximum": f"less than {value}",
        "minLength": f"at least {value} characters", "maxLength": f"at most {value} characters",
        "minItems": f"at least {value} items", "maxItems": f"at most {value} items",
        "pattern": f"pattern {value}", "multipleOf": f"multiple of {value}", "format": f"format {value}",
    }
    return texts.get(key, f"{key}={value}")


def _base_type(t):
    origin = typing.get_origin(t)
    if origin in (Union, types.UnionType):
        args = [a for a in typing.get_args(t) if a is not type(None)]
        return _base_type(args[0]) if len(args) == 1 else None
    if origin in (list, tuple, list):
        return list
    if t in (list, tuple):
        return list
    return t


def _schema(t, strict):
    if t is Any or t is object:
        return {}
    if t is str:
        return {"type": "string"}
    if t is bool:
        return {"type": "boolean"}
    if t is int:
        return {"type": "integer"}
    if t is float:
        return {"type": "number"}
    if t is type(None) or t is None:
        return {"type": "null"}
    if t in (list, tuple):
        return {"type": "array", "items": {}}
    if t is dict:
        return {"type": "object"}
    if is_schema_class(t):
        properties, required = {}, []
        for name, spec in t._fields().items():
            properties[name] = json_schema(spec.type, strict, spec.meta)
            if spec.required:
                required.append(name)
        s = {"type": "object", "properties": properties, "required": required, "additionalProperties": False}
        if t.__doc__:
            s["description"] = " ".join(t.__doc__.split())
        return s
    if is_pydantic_model(t):
        return _pydantic_schema(t, strict)
    origin = typing.get_origin(t)
    args = typing.get_args(t)
    if origin is typing.Annotated:
        return _schema(args[0], strict)
    if origin is Literal:
        values = list(args)
        s = {"enum": values}
        if all(isinstance(v, str) for v in values):
            s["type"] = "string"
        elif all(isinstance(v, bool) for v in values):
            s["type"] = "boolean"
        elif all(isinstance(v, int) for v in values):
            s["type"] = "integer"
        return s
    if origin in (list, tuple, list):
        item = args[0] if args else Any
        return {"type": "array", "items": _schema(item, strict)}
    if origin in (dict, dict):
        value_type = args[1] if len(args) == 2 else Any
        return {"type": "object", "additionalProperties": _schema(value_type, strict)}
    if origin in (Union, types.UnionType):
        return {"anyOf": [_schema(a, strict) for a in args]}
    raise SchemaError(f"unsupported type: {type_name(t)}")


def output_schema(t, strict: bool = False) -> dict:
    """Object schema for the return type of an agent method (non-object types are wrapped in one field)."""
    if is_object_type(t):
        return json_schema(t, strict)
    return {
        "type": "object",
        "properties": {RESULT_KEY: json_schema(t, strict)},
        "required": [RESULT_KEY],
        "additionalProperties": False,
    }


# ---------------------------------------------------------------------------
# validation and conversion

def _short(value, n=40):
    text = repr(value)
    return text if len(text) <= n else text[: n - 1] + "…"


def _path(path, part):
    if not path:
        return str(part)
    return f"{path}[{part}]" if isinstance(part, int) else f"{path}.{part}"


_INT_RE = re.compile(r"^\s*-?\d+\s*$")
_FLOAT_RE = re.compile(r"^\s*-?\d+(\.\d+)?([eE][-+]?\d+)?\s*$")


def _expected(where, what, value) -> str:
    return f"{where}: expected {what}, got {_short(value)}"


def convert(t, value, path: str, errors: list[str]):
    """Converts `value` to type `t`; mismatches are appended to `errors`."""
    where = path or "value"
    if t is Any or t is object:
        return value
    if t is type(None) or t is None:
        if value is None:
            return None
        errors.append(_expected(where, "None", value))
        return None
    if t is str:
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        errors.append(_expected(where, "str", value))
        return None
    if t is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false", "yes", "no"):
            return value.strip().lower() in ("true", "yes")
        errors.append(_expected(where, "bool", value))
        return None
    if t is int:
        if isinstance(value, bool):
            errors.append(_expected(where, "int", value))
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and _INT_RE.match(value):
            return int(value)
        errors.append(_expected(where, "int", value))
        return None
    if t is float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, str) and _FLOAT_RE.match(value):
            return float(value)
        errors.append(_expected(where, "float", value))
        return None
    if t in (list, tuple):
        if isinstance(value, (list, tuple)):
            return list(value)
        errors.append(_expected(where, "a list", value))
        return None
    if t is dict:
        if isinstance(value, dict):
            return value
        errors.append(_expected(where, "an object", value))
        return None
    if is_schema_class(t):
        if isinstance(value, t):
            return value
        if not isinstance(value, dict):
            errors.append(_expected(where, f"a {t.__name__} object", value))
            return None
        fields = _validate_object(t, value, path, errors)
        obj = object.__new__(t)
        for name, v in fields.items():
            object.__setattr__(obj, name, v)
        return obj
    if is_pydantic_model(t):
        return _pydantic_validate(t, value, path, errors)
    origin = typing.get_origin(t)
    args = typing.get_args(t)
    if origin is typing.Annotated:
        return convert(args[0], value, path, errors)
    if origin is Literal:
        if value in args and type(value) in {type(a) for a in args}:
            return value
        if isinstance(value, str):
            for option in args:
                if isinstance(option, str) and option.casefold() == value.strip().casefold():
                    return option
        errors.append(f"{where}: expected one of {type_name(t)}, got {_short(value)}")
        return None
    if origin in (list, tuple, list):
        if not isinstance(value, (list, tuple)):
            errors.append(_expected(where, type_name(t), value))
            return None
        item = args[0] if args else Any
        return [convert(item, v, _path(path, i), errors) for i, v in enumerate(value)]
    if origin in (dict, dict):
        if not isinstance(value, dict):
            errors.append(_expected(where, type_name(t), value))
            return None
        value_type = args[1] if len(args) == 2 else Any
        return {str(k): convert(value_type, v, _path(path, k), errors) for k, v in value.items()}
    if origin in (Union, types.UnionType):
        if value is None and type(None) in args:
            return None
        first_errors = None
        for option in args:
            if option is type(None):
                continue
            attempt: list[str] = []
            result = convert(option, value, path, attempt)
            if not attempt:
                return result
            first_errors = first_errors or attempt
        errors.extend(first_errors or [_expected(where, type_name(t), value)])
        return None
    errors.append(f"{where}: unsupported type {type_name(t)}")
    return None


def _validate_object(cls, data: dict, path: str, errors: list[str]) -> dict:
    result = {}
    fields = cls._fields()
    for name, spec in fields.items():
        sub_path = _path(path, name)
        if name not in data:
            if spec.required:
                errors.append(f"{sub_path}: required field missing ({type_name(spec.type)})")
                continue
            result[name] = _copy_default(spec.default)
            continue
        before = len(errors)
        value = convert(spec.type, data[name], sub_path, errors)
        if len(errors) == before and spec.meta is not None:
            _check_limits(value, spec.meta, sub_path, errors)
        result[name] = value
    unknown = [k for k in data if k not in fields]
    if unknown:
        errors.append(f"{path or cls.__name__}: unknown field(s): {', '.join(map(str, unknown))}")
    return result


def _copy_default(value):
    if isinstance(value, (list, dict, set)):
        return copy.deepcopy(value)
    return value


def _check_limits(value, meta: FieldInfo, path: str, errors: list[str]):
    if value is None:
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        size, unit = value, ""
    elif isinstance(value, (str, list)):
        size = len(value)
        unit = " characters" if isinstance(value, str) else " items"
    else:
        return
    if meta.min is not None and size < meta.min:
        errors.append(f"{path}: must be at least {meta.min}{unit}, got {size}{unit}")
    if meta.max is not None and size > meta.max:
        errors.append(f"{path}: must be at most {meta.max}{unit}, got {size}{unit}")
    if meta.pattern and isinstance(value, str) and not re.fullmatch(meta.pattern, value):
        errors.append(f"{path}: does not match pattern '{meta.pattern}'")


# ---------------------------------------------------------------------------
# parsing model output

def extract_json(text: str):
    """Extracts a JSON value from model text (tolerates code fences and text before/after the JSON)."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                value, _ = decoder.raw_decode(text[i:])
                return value
            except json.JSONDecodeError:
                continue
    raise ValueError("no valid JSON found in the response")


def _unwrap_result(return_type, data):
    if not is_object_type(return_type) and isinstance(data, dict) and len(data) == 1:
        key = next(iter(data))
        if key == RESULT_KEY:
            return data[key]
    return data


def parse_output(return_type, text: str):
    """Converts model text to the return type. Raises SchemaError on mismatch."""
    if return_type is str:
        return text.strip()
    try:
        data = extract_json(text)
    except ValueError as e:
        raise SchemaError([str(e)]) from e
    data = _unwrap_result(return_type, data)
    errors: list[str] = []
    result = convert(return_type, data, "", errors)
    if errors:
        raise SchemaError(errors)
    return result


def from_json(return_type, data):
    """Rebuilds a return value from the JSON value stored in a run record."""
    if return_type is str:
        return data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    errors: list[str] = []
    result = convert(return_type, data, "", errors)
    if errors:
        raise SchemaError(errors)
    return result
