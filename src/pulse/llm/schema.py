"""Strict JSON schemas for structured outputs (T3.2).

Turns a Pydantic model's JSON schema into one the structured-outputs API accepts and enforces
strictly: `$ref`s inlined, `additionalProperties: false` on every object, every property
required, and validation keywords the API doesn't enforce (lengths, ranges, counts) removed.
Those limits are enforced by the Pydantic validators instead.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

# Keywords dropped from the schema sent to the API. Their rules stay in the Pydantic model.
UNSUPPORTED_KEYWORDS = frozenset(
    {
        "title",
        "default",
        "examples",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
        "pattern",
        "format",
    }
)


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    raw = model.model_json_schema()
    defs: dict[str, Any] = raw.pop("$defs", {})
    result = _strict(raw, defs)
    assert isinstance(result, dict)
    return result


def _strict(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, list):
        return [_strict(item, defs) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        name = node["$ref"].rsplit("/", 1)[-1]
        return _strict(defs[name], defs)
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "properties":
            # Field names are data, not keywords: keep every one (a field may be named "title").
            out[key] = {name: _strict(schema, defs) for name, schema in value.items()}
        elif key not in UNSUPPORTED_KEYWORDS:
            out[key] = _strict(value, defs)
    if out.get("type") == "object" or "properties" in out:
        properties = out.setdefault("properties", {})
        out["required"] = list(properties)
        out["additionalProperties"] = False
    return out
