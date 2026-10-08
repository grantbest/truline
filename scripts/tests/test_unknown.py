"""Tests for the shared "could not be computed" primitive.

No substrate, no cluster, no network: everything here is a plain Python
object and ``json.dumps``/``json.loads`` -- the same serialization step a
real HTTP round trip performs.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location("unknown", REPO / "scripts" / "unknown.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


unknown = _load()


def test_unknown_is_distinguishable_from_zero_and_empty_in_process():
    sentinel = unknown.Unknown(reason="could not reach the substrate")

    assert unknown.is_unknown(sentinel) is True
    for flattened in (0, 0.0, "", None, [], {}, False):
        assert sentinel != flattened
        assert unknown.is_unknown(flattened) is False


def test_unknown_survives_a_full_json_round_trip():
    """The whole-path property: encode, transport (dumps/loads), decode."""
    sentinel = unknown.Unknown(reason="release charter not mirrored yet")

    encoded = unknown.to_jsonable(sentinel)
    wire = json.dumps(encoded)
    transported = json.loads(wire)
    decoded = unknown.from_jsonable(transported)

    assert unknown.is_unknown(decoded) is True
    assert decoded == sentinel
    # The transported JSON itself must not collapse to any zero/empty shape.
    assert transported not in (0, 0.0, "", None, [], {})


def test_a_genuine_zero_stays_zero_through_the_same_path():
    """A real, computed zero must never be relabelled unknown by this module."""
    payload = {"count": 0, "percent": 0.0, "items": []}

    encoded = unknown.to_jsonable(payload)
    transported = json.loads(json.dumps(encoded))
    decoded = unknown.from_jsonable(transported)

    assert decoded == payload
    assert unknown.is_unknown(decoded["count"]) is False
    assert unknown.is_unknown(decoded["percent"]) is False
    assert unknown.is_unknown(decoded["items"]) is False


def test_unknown_nested_inside_a_larger_structure_still_round_trips():
    payload = {
        "balance": [{"work_class": "security", "actual_pct": None}],
        "criteria": [{"ref": "PC-X-001/AC-1", "latest": unknown.Unknown(reason="malformed ref")}],
    }

    transported = json.loads(json.dumps(unknown.to_jsonable(payload)))
    decoded = from_result = unknown.from_jsonable(transported)

    assert decoded["balance"][0]["actual_pct"] is None
    assert unknown.is_unknown(decoded["criteria"][0]["latest"]) is True
    assert from_result["criteria"][0]["latest"] == unknown.Unknown(reason="malformed ref")
