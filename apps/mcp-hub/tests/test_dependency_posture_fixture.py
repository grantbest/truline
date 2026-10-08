"""Parity check (dev.finding 682ac674, part b): ``impact._depends_on_posture``
must classify the committed cross-app fixture exactly the way
``apps/factory-dispatcher/activities/ea_dependency.py``'s
``summarize_dependency_posture`` does, proven over a fixture that is
byte-identical in both apps' test trees
(``apps/factory-dispatcher/tests/fixtures/dependency-posture-fixture.json``,
pinned to this copy by ``scripts/tests/test_dependency_posture_fixture_drift.py``).
The hub cannot import the dispatcher module at runtime (PRIN-019), so this
fixture is the one thing that proves the two classifiers agree.

Vocabulary mapping, stated once, here only: the fixture's `expected` section
uses ea_dependency's three buckets (known / assessed_none / unknown).
``impact._depends_on_posture`` only has two (known / unknown) -- it collapses
ea_dependency's `known` and `assessed_none` into its own `known` on purpose
(impact.py's own docstring, :165-168: an application-to-application edge OR
an active assessed-none observation both mean "known" to impact). So an
application the fixture expects `known` OR `assessed_none` must read
"known" here; one the fixture expects `unknown` (including the coherence
case, which ea_dependency counts in `unknown`) must read "unknown".
"""

from __future__ import annotations

import json
from pathlib import Path

from tools import impact

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "dependency-posture-fixture.json"


def _load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text())


def test_depends_on_posture_agrees_with_ea_dependency_on_the_committed_fixture():
    fixture = _load_fixture()
    applications = fixture["applications"]
    app_by_id = {app["id"]: app for app in applications}

    dep_forward: dict[str, set] = {}
    for link in fixture["links"]:
        if link["link_type"] == "depends_on":
            dep_forward.setdefault(link["source_id"], set()).add(link["target_id"])

    assessed_none_obs_refs = {
        obs["content"]["ref"] for obs in fixture["observations"] if obs.get("state") == "active"
    }

    expected = fixture["expected"]
    ref_by_id = {app["id"]: app["content"]["ref"] for app in applications}

    for app_id, app in app_by_id.items():
        app_ref = ref_by_id[app_id]
        posture = impact._depends_on_posture(
            app_id,
            app_ref,
            dep_forward=dep_forward,
            app_by_id=app_by_id,
            assessed_none_obs_refs=assessed_none_obs_refs,
        )
        bucket = expected[app_ref]["bucket"]
        if bucket in ("known", "assessed_none"):
            assert posture == "known", f"{app_ref}: expected known (ea_dependency bucket {bucket!r}), got {posture!r}"
        else:
            assert posture == "unknown", f"{app_ref}: expected unknown, got {posture!r}"

    coherence_refs = {ref for ref, e in expected.items() if e["coherence_case"]}
    assert coherence_refs, "fixture must declare at least one coherence-case application"
    for app_id, app in app_by_id.items():
        app_ref = ref_by_id[app_id]
        if app_ref in coherence_refs:
            posture = impact._depends_on_posture(
                app_id,
                app_ref,
                dep_forward=dep_forward,
                app_by_id=app_by_id,
                assessed_none_obs_refs=assessed_none_obs_refs,
            )
            assert posture == "unknown"
