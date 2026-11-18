"""Validation against the engine's JSON Schemas in loopctl/schemas/ (build-plan G4). Standard library only.

Supports the subset the schemas use: type (incl. lists of types), enum, const, pattern, minLength, maxLength,
minimum, maximum, items, minItems, maxItems, properties, required, additionalProperties (bool or schema), anyOf.
Custom annotations: `x-stamped` (written by loopctl's clock, never by a caller) and `x-owner-clock` (the owner's
browser time on a command, used only to order the owner's own commands). Proof test T-TIME checks both.
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path
from typing import Any

SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"
NAMES = ("command", "events", "finding", "gate_result", "install", "state")


class SchemaError(ValueError):
    """A document doesn't match its schema; the message gives the path to the first problem."""


@cache
def load(name: str) -> dict[str, Any]:
    if name not in NAMES:
        raise KeyError(name)
    with open(SCHEMA_DIR / f"{name}.json", encoding="utf-8") as f:
        return json.load(f)


_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def errors(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Every problem, as "path: message"; empty when the value matches."""
    out: list[str] = []
    if "anyOf" in schema:
        if all(errors(value, s, path) for s in schema["anyOf"]):
            out.append(f"{path}: matches none of the allowed forms")
        return out
    if "const" in schema and value != schema["const"]:
        return [f"{path}: must be {schema['const']!r}"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path}: must be one of {schema['enum']!r}, got {value!r}"]
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_TYPES[t](value) for t in types):
            return [f"{path}: must be {' or '.join(types)}, got {type(value).__name__}"]
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            out.append(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            out.append(f"{path}: longer than {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            out.append(f"{path}: doesn't match {schema['pattern']}")
    if _TYPES["number"](value):
        if "minimum" in schema and value < schema["minimum"]:
            out.append(f"{path}: below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            out.append(f"{path}: above {schema['maximum']}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            out.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            out.append(f"{path}: more than {schema['maxItems']} items")
        if "items" in schema:
            for i, item in enumerate(value):
                out += errors(item, schema["items"], f"{path}[{i}]")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                out.append(f"{path}: missing {key}")
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in props:
                out += errors(item, props[key], f"{path}.{key}")
            elif extra is False:
                out.append(f"{path}: unknown field {key}")
            elif isinstance(extra, dict):
                out += errors(item, extra, f"{path}.{key}")
    return out


def validate(value: Any, name: str) -> None:
    """Raise SchemaError unless `value` matches schema `name` (events are checked with `validate_event`)."""
    if name == "events":
        return validate_event(value)
    problems = errors(value, load(name))
    if problems:
        raise SchemaError(f"{name}: " + "; ".join(problems[:5]))


def validate_event(event: Any) -> None:
    doc = load("events")
    problems = errors(event, doc["envelope"])
    if not problems:
        problems = errors(event["payload"], doc["payloads"][event["type"]], "$.payload")
    if problems:
        raise SchemaError("event: " + "; ".join(problems[:5]))


def stamped_fields(schema: Any, path: str = "$") -> list[str]:
    """Paths of every property marked `x-stamped` (written by loopctl's clock)."""
    found: list[str] = []
    if isinstance(schema, dict):
        if schema.get("x-stamped"):
            found.append(path)
        for key, sub in schema.get("properties", {}).items():
            found += stamped_fields(sub, f"{path}.{key}")
        for word in ("items", "additionalProperties"):
            if isinstance(schema.get(word), dict):
                found += stamped_fields(schema[word], f"{path}[]")
        for i, sub in enumerate(schema.get("anyOf", [])):
            found += stamped_fields(sub, path)
        if "payloads" in schema:
            for t, sub in schema["payloads"].items():
                found += stamped_fields(sub, f"{path}<{t}>")
        if "envelope" in schema:
            found += stamped_fields(schema["envelope"], path)
    return found
