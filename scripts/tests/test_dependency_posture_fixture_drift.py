"""The dependency-posture fixture is committed twice -- once in
``apps/factory-dispatcher/tests/fixtures/`` and once in
``apps/mcp-hub/tests/fixtures/`` -- because the hub cannot import the
dispatcher module at runtime (PRIN-019) to share it directly. A copy with no
guard drifts the first time one side is edited alone, exactly the failure
mode ``test_csdm_publish_tree_drift.py`` guards against for the published
CSDM tree. The rule this test enforces: never edit one copy alone, edit the
dispatcher's (the source) and re-copy (dev.finding 682ac674, part b).
"""

from __future__ import annotations

import json
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]
SOURCE = REPO / "apps" / "factory-dispatcher" / "tests" / "fixtures" / "dependency-posture-fixture.json"
COPY = REPO / "apps" / "mcp-hub" / "tests" / "fixtures" / "dependency-posture-fixture.json"


def test_dependency_posture_fixture_is_byte_identical_in_both_apps():
    assert SOURCE.read_bytes() == COPY.read_bytes(), (
        f"{COPY.relative_to(REPO)} has drifted from {SOURCE.relative_to(REPO)} -- "
        f"copy from {SOURCE.relative_to(REPO)} (the dispatcher's copy is the source), never edit the hub's directly."
    )


def test_dependency_posture_fixture_expected_keys_match_declared_applications():
    fixture = json.loads(SOURCE.read_text())
    declared_refs = {app["content"]["ref"] for app in fixture["applications"]}
    expected_refs = set(fixture["expected"])
    assert expected_refs == declared_refs, (
        "fixture['expected'] keys must equal the set of application refs declared in "
        f"fixture['applications'] -- declared-but-unexpected: {declared_refs - expected_refs}, "
        f"expected-but-undeclared: {expected_refs - declared_refs}"
    )
