#!/usr/bin/env python3
"""Unit tests for the minimal JSON Schema validator used by the contract."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))

from admin_contract_validator import load_schema, validate, validate_document  # noqa: E402


class TypeTests(unittest.TestCase):
    def test_string_accepts_string_rejects_integer(self):
        self.assertEqual(validate("hi", {"type": "string"}), [])
        self.assertEqual(
            validate(7, {"type": "string"}),
            ["$: expected string, got integer"],
        )

    def test_union_with_null_accepts_null_rejects_wrong_type(self):
        schema = {"type": ["string", "null"]}
        self.assertEqual(validate(None, schema), [])
        self.assertEqual(validate("hi", schema), [])
        self.assertEqual(
            validate(3, schema),
            ["$: expected string|null, got integer"],
        )

    def test_integer_rejects_bool_and_float(self):
        self.assertEqual(validate(3, {"type": "integer"}), [])
        self.assertEqual(
            validate(True, {"type": "integer"}),
            ["$: expected integer, got boolean"],
        )
        self.assertEqual(
            validate(1.5, {"type": "integer"}),
            ["$: expected integer, got number"],
        )

    def test_error_path_nests_into_properties(self):
        self.assertEqual(
            validate({"a": {"b": 1}}, {"properties": {"a": {"type": "string"}}}),
            ["$.a: expected string, got object"],
        )


class RequiredTests(unittest.TestCase):
    def test_present_passes_missing_fails(self):
        schema = {"type": "object", "required": ["a"]}
        self.assertEqual(validate({"a": 1}, schema), [])
        self.assertEqual(validate({}, schema), ["$: missing required key 'a'"])


class AdditionalPropertiesTests(unittest.TestCase):
    def test_stray_key_rejected_with_path(self):
        schema = {"type": "object", "properties": {"a": {}}, "additionalProperties": False}
        self.assertEqual(validate({"a": 1}, schema), [])
        self.assertEqual(
            validate({"a": 1, "x": 1}, schema),
            ["$: unexpected key 'x'"],
        )


class ItemsTests(unittest.TestCase):
    def test_each_item_validated_with_index_path(self):
        schema = {"items": {"type": "string"}}
        self.assertEqual(validate(["a", "b"], schema), [])
        self.assertEqual(
            validate(["a", 2], schema),
            ["$[1]: expected string, got integer"],
        )


class EnumTests(unittest.TestCase):
    def test_member_passes_non_member_fails(self):
        schema = {"enum": ["a", "b"]}
        self.assertEqual(validate("a", schema), [])
        self.assertEqual(
            validate("c", schema),
            ["$: 'c' not in enum ['a', 'b']"],
        )

    def test_enum_with_null_allows_null(self):
        self.assertEqual(validate(None, {"enum": ["a", None]}), [])


class ConstTests(unittest.TestCase):
    def test_match_passes_mismatch_fails(self):
        self.assertEqual(validate("queue", {"const": "queue"}), [])
        self.assertEqual(
            validate("other", {"const": "queue"}),
            ["$: expected const 'queue', got 'other'"],
        )


class AnyOfTests(unittest.TestCase):
    def test_branch_match_passes(self):
        schema = {"anyOf": [{"type": "string"}, {"type": "integer"}]}
        self.assertEqual(validate("a", schema), [])
        self.assertEqual(validate(3, schema), [])

    def test_no_branch_match_reports_count_and_closest_errors(self):
        schema = {
            "anyOf": [
                {"type": "object", "required": ["a"], "additionalProperties": False},
                {"type": "object", "required": ["b"], "additionalProperties": False},
            ]
        }
        errors = validate({"c": 1}, schema)
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("$: failed all 2 anyOf branches;"))
        # The closest branch (fewest errors) is reported verbatim.
        self.assertIn(
            "closest branch errors: [\"$: missing required key 'a'\", "
            "\"$: unexpected key 'c'\"]",
            errors[0],
        )

    def test_branch_match_short_circuits_bad_local_keywords(self):
        schema = {
            "type": "object",
            "anyOf": [{"type": "object"}, {"type": "string"}],
            "required": ["a"],
        }
        # The anyOf branch matches, but the local required still applies.
        self.assertEqual(validate({}, schema), ["$: missing required key 'a'"])


class RefTests(unittest.TestCase):
    def test_ref_to_sibling_file_by_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "inner.schema.json").write_text(
                json.dumps({"type": "object", "required": ["v"]}), encoding="utf-8"
            )
            outer = {"properties": {"data": {"$ref": "inner.schema.json"}}}
            self.assertEqual(validate({"data": {"v": 1}}, outer, base_dir=base), [])
            self.assertEqual(
                validate({"data": {}}, outer, base_dir=base),
                ["$.data: missing required key 'v'"],
            )

    def test_validate_document_resolves_contract_ref_chain(self):
        """$ref in sse_event.schema.json resolves to the real sibling schema."""
        instance = {
            "event": "queue",
            "data": {
                "open": [],
                "count": 0,
                "recent": [{"bogus": True}],
                "generated_at": "2026-09-23T12:00:00+00:00",
            },
        }
        errors = validate_document(instance, "sse_event.schema.json")
        self.assertIn(
            "$.data.recent[0]: missing required key 'request_id'", errors
        )


class PatternTests(unittest.TestCase):
    SCHEMA = {"type": "string", "pattern": "^a+$"}

    def test_matching_string_passes(self):
        self.assertEqual(validate("aaa", self.SCHEMA), [])

    def test_non_matching_string_is_reported_with_its_path(self):
        self.assertEqual(
            validate("ab", self.SCHEMA, path="$.next"),
            ["$.next: 'ab' does not match pattern '^a+$'"],
        )

    def test_null_in_a_string_or_null_union_skips_the_pattern(self):
        self.assertEqual(validate(None, {"type": ["string", "null"], "pattern": "^a$"}), [])

    def test_recent_page_next_and_decided_at_are_pinned(self):
        row = {
            "request_id": "r1", "container": "c", "host": "h.example.com", "port": 443,
            "status": "allowed", "scope": "live", "decided_at": "2026-09-23T12:00:00Z",
            "decided_by": "operator", "apply_status": None, "deny_reason": None,
        }
        good = {"rows": [row], "next": "2026-09-23T12:00:00Z,r1"}
        self.assertEqual(validate_document(good, "recent_page.schema.json"), [])
        for bad_next in ("2026-09-23T12:00:00Z", "r1", "2026-09-23,r1", ""):
            with self.subTest(next=bad_next):
                self.assertTrue(
                    validate_document({"rows": [row], "next": bad_next}, "recent_page.schema.json")
                )
        bad_row = dict(row, decided_at="2026-09-23 12:00")
        self.assertTrue(
            validate_document({"rows": [bad_row], "next": None}, "recent_page.schema.json")
        )


class ContractSchemasAreValidJsonTests(unittest.TestCase):
    def test_every_contract_schema_parses(self):
        for name in (
            "queue_snapshot.schema.json",
            "decide_response.schema.json",
            "recent_page.schema.json",
            "sse_event.schema.json",
        ):
            with self.subTest(schema=name):
                self.assertIsInstance(load_schema(name), dict)


if __name__ == "__main__":
    unittest.main()
