#!/usr/bin/env python3
"""Minimal JSON Schema validator for the admin contract schemas.

Stdlib-only. Covers exactly the draft-2020-12 subset the schemas in
admin/contract/ use: type (including unions with null), required,
properties, additionalProperties: false, items, enum, const, anyOf (valid
when any branch has zero errors; on failure one error names the branch count
and the errors of the closest branch), and $ref to a sibling schema file (by bare filename). validate() returns a list of error
strings, each with the JSON path of the offending value, e.g.
"$.open[0].last_error: expected string|null, got integer".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CONTRACT_DIR = Path(__file__).resolve().parent.parent / "admin" / "contract"

_TYPE_CHECKS: dict[str, Any] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def load_schema(filename: str, *, base_dir: Path | None = None) -> dict[str, Any]:
    """Load one contract schema file by name from the contract directory."""
    directory = base_dir if base_dir is not None else CONTRACT_DIR
    return json.loads((directory / filename).read_text(encoding="utf-8"))


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _check_type(value: Any, schema: dict[str, Any], path: str) -> list[str]:
    expected = schema.get("type")
    if expected is None:
        return []
    names = [expected] if isinstance(expected, str) else list(expected)
    if any(_TYPE_CHECKS[name](value) for name in names):
        return []
    want = "|".join(names)
    return [f"{path}: expected {want}, got {_type_name(value)}"]


def validate(
    instance: Any,
    schema: dict[str, Any],
    *,
    path: str = "$",
    base_dir: Path | None = None,
) -> list[str]:
    """Validate instance against the supported schema subset; list errors."""
    if "$ref" in schema:
        target = load_schema(schema["$ref"], base_dir=base_dir or CONTRACT_DIR)
        return validate(instance, target, path=path, base_dir=base_dir or CONTRACT_DIR)

    errors = _check_type(instance, schema, path)
    if errors:
        return errors  # further checks are meaningless on a type mismatch

    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}, got {instance!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} not in enum {schema['enum']!r}")

    if "anyOf" in schema:
        branch_errors = [
            validate(instance, branch, path=path, base_dir=base_dir)
            for branch in schema["anyOf"]
        ]
        if all(branch_errors):
            closest = min(branch_errors, key=len)
            errors.append(
                f"{path}: failed all {len(branch_errors)} anyOf branches; "
                f"closest branch errors: {closest}"
            )
            return errors
        # A branch matched: local keywords below still apply.

    if isinstance(instance, dict):
        for key, sub in schema.get("properties", {}).items():
            if key in instance:
                errors.extend(
                    validate(instance[key], sub, path=f"{path}.{key}", base_dir=base_dir)
                )
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing required key {key!r}")
        extra = schema.get("additionalProperties")
        if extra is False:
            for key in instance:
                if key not in schema.get("properties", {}):
                    errors.append(f"{path}: unexpected key {key!r}")

    if isinstance(instance, list) and "items" in schema:
        for index, item in enumerate(instance):
            errors.extend(
                validate(item, schema["items"], path=f"{path}[{index}]", base_dir=base_dir)
            )

    return errors


def validate_document(
    instance: Any, filename: str, *, base_dir: Path | None = None
) -> list[str]:
    """Load a contract schema by filename and validate instance against it."""
    return validate(instance, load_schema(filename, base_dir=base_dir))
