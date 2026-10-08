"""build_content() is a whitelist, so anything not named in it is dropped.

The three traceability fields were dropped from the day they were added
(2026-08-02) until 2026-08-03: silently, with exit code 0. dev.task 93a1ab40 was
filed carrying two NFRs and an arch_impact and came back with none of them,
which made the renderer that puts them in the PR body unreachable in practice.
"""

from __future__ import annotations

import io
import json
import pathlib
import subprocess
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import file_task  # noqa: E402
import dispatch  # noqa: E402
import substrate  # noqa: E402
from beadstore import validate_candidate_bead  # noqa: E402
import bead_rules  # noqa: E402 - apps/substrate/src is on sys.path via beadstore


@pytest.fixture(autouse=True)
def _no_real_pristine_verification(monkeypatch):
    """Keep these tests hermetic now that filing verifies against main.

    ``file_task.main`` runs the declared verification in a fresh clone of
    ``main`` (OPS-7). That is the point of the feature and it has its own
    tests in test_file_task_pristine_verification.py, but it makes every
    ``main()`` test here clone the repository and run whole suites — and it
    fails outright wherever no local ``main`` ref exists, which is exactly
    what CI's detached-HEAD checkout gives you: eight tests in this file went
    red on `git rev-parse main` alone. These tests are about argument
    parsing, duplicate detection and predecessor checks, so the clone is
    stubbed out rather than performed.
    """
    monkeypatch.setattr(
        dispatch,
        "verify_pristine_commands",
        lambda commands, cfg=None: dispatch.VerificationReport(()),
    )


def _spec(**overrides):
    spec = {
        "lane": "drift",
        "title": "t",
        "intent": "i",
        "acceptance": ["THE thing SHALL happen"],
        "scope": {"paths": ["apps/x/"]},
        "risk_class": "behavioral",
        # Filing refuses work that cites no requirement (the Operator's decision
        # 2026-08-08, on TF-1's count). These fixtures exercise everything ELSE
        # about build_content, so they carry the waiver rather than a reference:
        # a waiver populates none of requirement_refs, nfrs or arch_impact, so
        # every assertion below still tests exactly what it tested before.
        "requirement_refs_waived": "test fixture; exercises unrelated behaviour",
        # Filing also refuses work naming no release (2026-08-26's release
        # traceability task, cloned one field over from requirement_refs
        # above). These fixtures are not about that either, so they carry the
        # waiver rather than a ref.
        "release_ref_waived": "test fixture; exercises unrelated behaviour",
    }
    spec.update(overrides)
    return spec


def test_requirement_refs_survive():
    content = file_task.build_content(_spec(requirement_refs=["LO-CAT-004/AC-1"]))
    assert content["requirement_refs"] == ["LO-CAT-004/AC-1"]


def test_nfrs_survive_with_all_four_fields():
    nfr = {
        "category": "latency",
        "statement": "the board loads promptly",
        "threshold": "p95 < 800ms",
        "verification": "k6 run against the console",
    }
    content = file_task.build_content(_spec(nfrs=[nfr]))
    assert content["nfrs"] == [nfr]
    # threshold and verification are what make an NFR testable rather than a
    # comment; losing either in transit would be worse than losing the whole field.
    assert content["nfrs"][0]["threshold"] == "p95 < 800ms"
    assert content["nfrs"][0]["verification"] == "k6 run against the console"


def test_arch_impact_survives():
    impact = {
        "applications": ["app.factory-dispatcher"],
        "capabilities": ["pc.factory.dispatch"],
        "notes": "closes the carrier gap",
    }
    content = file_task.build_content(_spec(arch_impact=impact))
    assert content["arch_impact"] == impact


def test_all_three_together():
    content = file_task.build_content(_spec(
        requirement_refs=["LO-CAT-004"],
        nfrs=[{"category": "cost", "statement": "s", "threshold": "t", "verification": "v"}],
        arch_impact={"applications": ["app.x"], "capabilities": []},
    ))
    for field in ("requirement_refs", "nfrs", "arch_impact"):
        assert field in content, f"{field} was dropped"


def test_source_bead_ids_survive_as_structured_refile_provenance():
    content = file_task.build_content(_spec(source_bead_ids=["f8237e30"]))

    assert content["source_bead_ids"] == ["f8237e30"]


def test_predecessor_bead_ids_survive_as_structured_ordering_constraint():
    content = file_task.build_content(_spec(predecessor_bead_ids=["P10-A"]))

    assert content["predecessor_bead_ids"] == ["P10-A"]


def test_expect_pristine_failure_survives_in_verification_block():
    content = file_task.build_content(_spec(
        verification={
            "commands": ["python -m pytest tests/ -q"],
            "must_report_unverified": True,
            "expect_pristine_failure": True,
        },
    ))

    assert content["verification"]["expect_pristine_failure"] is True


def test_absent_fields_are_not_invented():
    """Writing an empty list is a different claim from writing nothing."""
    content = file_task.build_content(_spec())
    for field in ("requirement_refs", "nfrs", "arch_impact"):
        assert field not in content


def test_attempt_fields_are_not_written_to_new_tasks():
    content = file_task.build_content(_spec(max_attempts=9))

    assert "attempts" not in content
    assert "max_attempts" not in content


def test_empty_values_are_not_written():
    content = file_task.build_content(_spec(requirement_refs=[], nfrs=[], arch_impact=None))
    for field in ("requirement_refs", "nfrs", "arch_impact"):
        assert field not in content


def test_the_93a1ab40_regression():
    """The exact spec shape that lost its traceability on 2026-08-02."""
    content = file_task.build_content(_spec(
        nfrs=[
            {"category": "observability", "statement": "reviewer can derive suites",
             "threshold": "all present fields appear in the PR body",
             "verification": "open the PR and read it"},
            {"category": "usability", "statement": "no empty scaffolding",
             "threshold": "byte-identical body when absent",
             "verification": "equality test against current rendering"},
        ],
        arch_impact={
            "applications": ["app.factory-dispatcher"],
            "capabilities": ["pc.factory.dispatch", "pc.factory.collaboration"],
        },
    ))
    assert len(content["nfrs"]) == 2
    assert content["arch_impact"]["applications"] == ["app.factory-dispatcher"]


def test_forbidden_path_is_still_appended():
    """The pre-existing guarantee must not regress."""
    content = file_task.build_content(_spec())
    for always in file_task.FORBIDDEN_ALWAYS:
        assert always in content["scope"]["forbidden_paths"]


def test_missing_required_field_still_rejected():
    with pytest.raises(SystemExit):
        file_task.build_content({"lane": "drift"})


# ---------------------------------------------------------------------------
# R26.12 B16: the intake refuses the emergency marker fields, and refuses a
# spec whose scope.paths intersects forbidden_paths (the docs/releases/**
# charter fence in particular).
# ---------------------------------------------------------------------------


def test_marker_fields_are_exactly_the_three_named_fields():
    """Guards item 1's parametrization below: if this drifts, so does the coverage.

    This test names the three fields as literals, independently of
    ``file_task.MARKER_FIELDS``, so dropping one from the tuple shows up here
    even though the parametrized test below would otherwise lose its case
    silently along with the field it was covering.
    """
    assert set(file_task.MARKER_FIELDS) == {
        "class_of_service",
        "expedite_reason",
        "expedite_until",
    }


@pytest.mark.parametrize("field", ["class_of_service", "expedite_reason", "expedite_until"])
def test_intake_refuses_class_of_service(field):
    with pytest.raises(SystemExit) as exc:
        file_task.build_content(_spec(**{field: "emergency" if field == "class_of_service" else "x"}))
    message = str(exc.value)
    assert field in message
    assert "operator verb" in message


@pytest.mark.parametrize("field", ["class_of_service", "expedite_reason", "expedite_until"])
@pytest.mark.parametrize("empty_value", [None, ""])
def test_intake_refuses_marker_fields_even_when_falsy(field, empty_value):
    """A marker field that is present but empty is still a marker field.

    ``_check_no_marker_fields`` must refuse on ``field in spec``, not on
    ``spec.get(field)`` -- the latter would wave a ``None`` or ``""`` value
    through, which is exactly how a lane that merely forgot to populate the
    field (rather than one that never set it) would slip past.
    """
    with pytest.raises(SystemExit) as exc:
        file_task.build_content(_spec(**{field: empty_value}))
    message = str(exc.value)
    assert field in message
    assert "operator verb" in message


def test_intake_accepts_a_spec_carrying_none_of_the_marker_fields():
    content = file_task.build_content(_spec())
    for field in file_task.MARKER_FIELDS:
        assert field not in content


def test_forbidden_always_is_pinned():
    """Both copies converging on the wrong list is invisible to byte-parity
    checks (item 3 of the requeue note) -- pin the literal contents here.
    """
    assert file_task.FORBIDDEN_ALWAYS[0] == ".github/workflows/**"
    assert "docs/releases/**" in file_task.FORBIDDEN_ALWAYS


def test_intake_refuses_docs_releases():
    with pytest.raises(SystemExit) as exc:
        file_task.build_content(
            _spec(scope={"paths": ["docs/releases/policy/health-policy.json"]})
        )
    message = str(exc.value)
    assert "docs/releases/policy/health-policy.json" in message
    assert "forbidden_always" in message


def test_forbidden_always_rejects_the_old_string_shape():
    with pytest.raises(SystemExit) as exc:
        file_task._coerce_forbidden_always(".github/workflows/**")
    assert "forbidden_always must be a list of strings" in str(exc.value)


# ---------------------------------------------------------------------------
# c2c52aaf AC-2: a spec that is not a JSON object is refused by name, before
# the REQUIRED check, instead of an AttributeError from spec.get.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_spec", [[], "x", None])
def test_build_content_refuses_non_dict_spec_by_name(bad_spec):
    with pytest.raises(SystemExit) as exc:
        file_task.build_content(bad_spec)
    assert str(exc.value) == f"spec must be a JSON object; got {type(bad_spec).__name__}"


def test_cli_refuses_non_dict_spec_via_stdin(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("[]"))
    with pytest.raises(SystemExit) as exc:
        file_task.main(["-"])
    assert str(exc.value) == "spec must be a JSON object; got list"


# ---------------------------------------------------------------------------
# predecessor supersession — the 2026-08-20 wedge (#457 pointed at held
# originals instead of their landed replacements; is_runnable refused them
# forever and nothing caught it at filing time) — and its 2026-09 gap: the
# check only ever consulted the reverse index (source_bead_ids), never the
# predecessor's own state, so OPS-107's predecessor (superseded by hand with
# no reverse pointer recorded) passed clean and sat unrunnable for ~12 hours.
# ---------------------------------------------------------------------------


class FakeSubstrate:
    """No network, no database — just enough of Substrate for main()'s wiring.

    ``beads``: beads that exist outside the dev.task population this double
    already models via ``self._tasks`` — e.g. pre-seeded dev.finding beads a
    derived_from_finding_ids test wants ``list_beads``/``add_link`` to see.
    Each entry must carry at least ``id``, ``namespace``, ``type`` and
    ``state``. Anything ``create_bead`` creates during a test is added here
    too, so both sources answer ``list_beads``/``transition_state``/
    ``add_link`` identically regardless of how the bead came to exist.
    """

    def __init__(self, tasks, transition_error=None, patch_error_for=None, beads=None):
        self._tasks = tasks
        self.posted = None
        self.notes_by_id: dict[str, list] = {}
        self.notes: list = []
        self.transitions: list = []
        self.patches: list = []
        self.links: list = []
        self._transition_error = transition_error
        self._patch_error_for = patch_error_for or set()
        self._beads: dict[str, dict] = {str(b["id"]): dict(b) for b in (beads or [])}

    def list_tasks(self, state=None, limit=200):
        return self._tasks

    def list_notes(self, parent_id, limit=500):
        return self.notes_by_id.get(parent_id, [])

    def add_note(self, parent_id, kind, body, created_by, **extra):
        note = {"parent_id": parent_id, "kind": kind, "body": body, "created_by": created_by}
        self.notes.append(note)
        return note

    def _current_state(self, bead_id):
        for task in self._tasks:
            if task.get("id") == bead_id:
                return task.get("state")
        bead = self._beads.get(bead_id)
        return bead.get("state") if bead else None

    def _set_state(self, bead_id, state):
        for task in self._tasks:
            if task.get("id") == bead_id:
                task["state"] = state
                return
        bead = self._beads.get(bead_id)
        if bead is not None:
            bead["state"] = state

    def transition_state(self, bead_id, from_state, to_state, created_by):
        if self._transition_error is not None:
            raise self._transition_error
        current = self._current_state(bead_id)
        if current is not None and current != from_state:
            raise substrate.SubstrateError(
                409, f"bead {bead_id} is in state {current!r}, not {from_state!r}"
            )
        self.transitions.append((bead_id, from_state, to_state, created_by))
        self._set_state(bead_id, to_state)
        return {"id": bead_id, "state": to_state}

    def _request(self, method, path, **kwargs):
        self.posted = kwargs.get("json")
        return {"id": "new-bead-id"}

    def create_task(self, content, created_by, *, trust_tier="user"):
        return self._request(
            "POST",
            "/beads",
            json={
                "namespace": "dev",
                "type": "task",
                "state": "pending",
                "trust_tier": trust_tier,
                "created_by": created_by,
                "content": content,
            },
        )

    def create_bead(
        self, namespace, type, state, content, created_by, *,
        trust_tier="user", context=None, provenance=None,
    ):
        """Mirrors the live store's own two checks (``BeadCreate`` shape —
        including the agent-authorship/provenance-key-set rules — and the
        namespace's content schema) by delegating to the identical model
        classes, rather than a second, hand-kept copy of the same five rules
        that could drift from them (dev.finding 24970f0e's whole point)."""
        try:
            validate_candidate_bead(
                namespace, type, content, provenance or {}, created_by,
                trust_tier=trust_tier, state=state,
            )
        except Exception as exc:
            raise substrate.SubstrateError(422, str(exc)) from exc
        bead_id = f"new-bead-{len(self._beads)}"
        self._beads[bead_id] = {
            "id": bead_id,
            "namespace": namespace,
            "type": type,
            "state": state,
            "content": dict(content),
        }
        return {"id": bead_id, "state": state}

    def list_beads(self, namespace, type, state=None, limit=200):
        if namespace == "dev" and type == "task":
            results = [dict(t) for t in self._tasks]
        else:
            results = [
                dict(b) for b in self._beads.values()
                if b.get("namespace") == namespace and b.get("type") == type
            ]
        if state is not None:
            results = [r for r in results if r.get("state") == state]
        return results[:limit]

    def add_link(self, source_id, target_id, link_type, created_by):
        # Target existence only, not source: every production caller passes
        # the just-created bead's own id as source_id, which this double
        # (like create_task below) never registers into self._tasks -- the
        # real store's FK check on BOTH ends is already proven in
        # apps/substrate/tests/test_bead_link.py; this double's job is the
        # target-existence backstop derived_from_finding_ids depends on
        # (file_task.py's _check_derived_from_findings docstring).
        known_ids = {t.get("id") for t in self._tasks} | set(self._beads)
        if target_id not in known_ids:
            raise substrate.SubstrateError(
                404, json.dumps({"error": "bead_not_found", "missing": [target_id]})
            )
        if link_type not in bead_rules.BEAD_LINK_TYPES:
            raise substrate.SubstrateError(422, f"unrecognised link_type {link_type!r}")
        self.links.append((source_id, target_id, link_type, created_by))
        return {"source_id": source_id, "target_id": target_id, "link_type": link_type}

    def patch_content(self, bead_id, content, created_by):
        if bead_id in self._patch_error_for:
            raise RuntimeError("409 conflict")
        self.patches.append((bead_id, content, created_by))
        for candidate in self._tasks:
            if candidate.get("id") == bead_id:
                candidate["content"] = content
        return {"id": bead_id, "content": content}


def _replacement(replacement_id, source_id):
    return {"id": replacement_id, "content": {"source_bead_ids": [source_id]}}


def _predecessor(bead_id, state):
    return {"id": bead_id, "state": state, "content": {}}


class TestPredecessorSupersession:
    def test_refuses_a_superseded_predecessor_with_a_recorded_replacement(self):
        predecessor = _predecessor("24f494d4", "superseded")
        replacement = _replacement("landed-repl", "24f494d4")

        with pytest.raises(SystemExit) as exc:
            file_task._check_predecessor_supersession(
                ["24f494d4"], [predecessor, replacement]
            )

        assert "24f494d4" in str(exc.value)
        assert "landed-repl" in str(exc.value)

    def test_names_every_superseding_bead(self):
        predecessor = _predecessor("24f494d4", "superseded")
        replacements = [
            _replacement("repl-a", "24f494d4"),
            _replacement("repl-b", "24f494d4"),
        ]

        with pytest.raises(SystemExit) as exc:
            file_task._check_predecessor_supersession(
                ["24f494d4"], [predecessor, *replacements]
            )

        assert "repl-a" in str(exc.value)
        assert "repl-b" in str(exc.value)

    def test_refuses_a_superseded_predecessor_with_no_recorded_replacement(self):
        """OPS-107's actual shape: 79a6fba7 was hand-superseded with no
        source_bead_ids pointer recorded on its replacement, so the reverse
        index (superseding_task_ids) finds nothing. The old check consulted
        only that index and filed clean; this one also looks at the
        predecessor's own state and must refuse regardless."""
        predecessor = _predecessor("79a6fba7", "superseded")

        with pytest.raises(SystemExit) as exc:
            file_task._check_predecessor_supersession(["79a6fba7"], [predecessor])

        message = str(exc.value)
        assert "79a6fba7" in message
        assert "can never land" in message

    def test_refuses_an_archived_predecessor(self):
        predecessor = _predecessor("arch-1", "archived")

        with pytest.raises(SystemExit) as exc:
            file_task._check_predecessor_supersession(["arch-1"], [predecessor])

        message = str(exc.value)
        assert "arch-1" in message
        assert "can never land" in message

    def test_refuses_a_predecessor_that_does_not_exist(self):
        with pytest.raises(SystemExit) as exc:
            file_task._check_predecessor_supersession(["ghost-1"], [])

        message = str(exc.value)
        assert "ghost-1" in message
        assert "no dev.task" in message

    def test_allows_a_predecessor_that_is_not_superseded(self):
        live = _predecessor("24f494d4", "pending")
        file_task._check_predecessor_supersession(["24f494d4"], [live])

    def test_allows_a_doing_predecessor(self):
        doing = _predecessor("24f494d4", "doing")
        file_task._check_predecessor_supersession(["24f494d4"], [doing])

    def test_allows_a_review_predecessor(self):
        review = _predecessor("24f494d4", "review")
        file_task._check_predecessor_supersession(["24f494d4"], [review])

    def test_allows_a_failed_predecessor(self):
        """A failed predecessor is requeueable, not dead — is_runnable and
        the dev.task state machine both allow failed -> pending/superseded,
        so this is not a state the guard can never release from."""
        failed = _predecessor("24f494d4", "failed")
        file_task._check_predecessor_supersession(["24f494d4"], [failed])

    def test_allows_a_done_predecessor(self):
        done = _predecessor("24f494d4", "done")
        file_task._check_predecessor_supersession(["24f494d4"], [done])

    def test_main_refuses_filing_against_a_held_predecessor(self, tmp_path, monkeypatch):
        spec = _spec(predecessor_bead_ids=["24f494d4"])
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(spec))

        fake = FakeSubstrate(
            [_predecessor("24f494d4", "superseded"), _replacement("landed-repl", "24f494d4")]
        )
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)

        with pytest.raises(SystemExit) as exc:
            file_task.main([str(spec_path)])

        assert "landed-repl" in str(exc.value)
        assert fake.posted is None

    def test_main_refuses_filing_against_a_held_predecessor_with_no_replacement(
        self, tmp_path, monkeypatch
    ):
        """The OPS-107 shape end-to-end: a superseded predecessor with no
        reverse pointer must still refuse filing, not just fail a unit test
        of the helper in isolation."""
        spec = _spec(predecessor_bead_ids=["79a6fba7"])
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(spec))

        fake = FakeSubstrate([_predecessor("79a6fba7", "superseded")])
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)

        with pytest.raises(SystemExit) as exc:
            file_task.main([str(spec_path)])

        message = str(exc.value)
        assert "79a6fba7" in message
        assert "can never land" in message
        assert fake.posted is None

    def test_main_files_normally_against_a_live_predecessor(self, tmp_path, monkeypatch):
        spec = _spec(predecessor_bead_ids=["24f494d4"])
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(spec))

        fake = FakeSubstrate([_predecessor("24f494d4", "pending")])
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)

        rc = file_task.main([str(spec_path)])

        assert rc == 0
        assert fake.posted is not None

    def test_main_skips_the_check_when_no_predecessors_are_declared(self, tmp_path, monkeypatch):
        spec = _spec()
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(spec))

        fake = FakeSubstrate([])
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)

        rc = file_task.main([str(spec_path)])

        assert rc == 0
        assert fake.posted is not None


# ---------------------------------------------------------------------------
# duplicate filing — the 2026-08-21 wedge (S24-P1/P2 filed twice; #477/#478
# spent two workers' full budgets before the gate caught them as duplicates
# of #479/#480). file_task.py now refuses a same-identity filing while a
# bead for it is still live, and requires --supersede to name a deliberate
# re-file.
# ---------------------------------------------------------------------------


def _failure_note(ordinal):
    """A recorded fail_task note, shaped as guards.prior_failures expects."""
    return {
        "id": f"note-{ordinal}",
        "created_at": f"2026-08-{10 + ordinal:02d}T00:00:00Z",
        "content": {"kind": "status", "body": f"Run failed: attempt {ordinal}"},
    }


class TestSpecIdentity:
    def test_a_repo_path_resolves_to_a_repo_relative_string(self):
        repo_root = pathlib.Path(file_task.__file__).resolve().parents[2]
        target = (
            repo_root
            / "apps"
            / "factory-dispatcher"
            / "tasks"
            / "S30-B3-merge-path-requires-recorded-verdict.json"
        )

        identity = file_task._spec_identity(str(target))

        assert identity == (
            "apps/factory-dispatcher/tasks/S30-B3-merge-path-requires-recorded-verdict.json"
        )

    def test_stdin_has_no_path_identity(self):
        assert file_task._spec_identity("-") is None

    def test_a_path_outside_the_repo_falls_back_to_the_absolute_path(self, tmp_path):
        outside = tmp_path / "spec.json"
        outside.write_text("{}")

        identity = file_task._spec_identity(str(outside))

        assert identity == str(outside.resolve())


class TestDuplicateFiling:
    def _file(self, tmp_path, **overrides):
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(_spec(**overrides)))
        return spec_path

    # --- the refusal ------------------------------------------------------

    def test_refuses_when_a_pending_duplicate_is_open(self, tmp_path, monkeypatch):
        existing = {"id": "existing-1", "state": "pending", "content": {"title": "t"}}
        fake = FakeSubstrate([existing])
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = self._file(tmp_path)

        with pytest.raises(SystemExit) as exc:
            file_task.main([str(spec_path)])

        assert "existing-1" in str(exc.value)
        assert "pending" in str(exc.value)
        assert fake.posted is None

    def test_refuses_when_a_doing_duplicate_is_open(self, tmp_path, monkeypatch):
        existing = {"id": "existing-2", "state": "doing", "content": {"title": "t"}}
        fake = FakeSubstrate([existing])
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = self._file(tmp_path)

        with pytest.raises(SystemExit) as exc:
            file_task.main([str(spec_path)])

        assert "existing-2" in str(exc.value)
        assert fake.posted is None

    def test_a_failed_duplicate_with_attempts_remaining_still_refuses(self, tmp_path, monkeypatch):
        existing = {"id": "existing-3", "state": "failed", "content": {"title": "t"}}
        fake = FakeSubstrate([existing])
        fake.notes_by_id["existing-3"] = [_failure_note(0)]  # 1 of 3 attempts spent
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = self._file(tmp_path)

        with pytest.raises(SystemExit) as exc:
            file_task.main([str(spec_path)])

        assert "existing-3" in str(exc.value)
        assert fake.posted is None

    def test_a_failed_duplicate_with_attempts_exhausted_is_not_a_duplicate(
        self, tmp_path, monkeypatch
    ):
        existing = {"id": "existing-4", "state": "failed", "content": {"title": "t"}}
        fake = FakeSubstrate([existing])
        fake.notes_by_id["existing-4"] = [_failure_note(i) for i in range(3)]
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = self._file(tmp_path)

        rc = file_task.main([str(spec_path)])

        assert rc == 0
        assert fake.posted is not None

    def test_matches_by_title_even_without_a_recorded_spec_identity(self, tmp_path, monkeypatch):
        """The legacy population (filed before this feature, or via stdin)
        carries no spec_identity field. Title alone must still catch it —
        that is exactly the signal that would have caught #477/#478."""
        existing = {"id": "existing-5", "state": "pending", "content": {"title": "t"}}
        fake = FakeSubstrate([existing])
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = self._file(tmp_path)

        with pytest.raises(SystemExit):
            file_task.main([str(spec_path)])

    def test_unrelated_title_and_path_files_normally(self, tmp_path, monkeypatch):
        existing = {"id": "existing-6", "state": "pending", "content": {"title": "unrelated"}}
        fake = FakeSubstrate([existing])
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = self._file(tmp_path)

        rc = file_task.main([str(spec_path)])

        assert rc == 0
        assert fake.posted is not None

    # --- done/completed beads are not duplicates ---------------------------

    def test_a_done_duplicate_is_not_a_duplicate(self, tmp_path, monkeypatch):
        existing = {"id": "existing-7", "state": "done", "content": {"title": "t"}}
        fake = FakeSubstrate([existing])
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = self._file(tmp_path)

        rc = file_task.main([str(spec_path)])

        assert rc == 0
        assert fake.posted is not None

    def test_an_archived_duplicate_is_not_a_duplicate(self, tmp_path, monkeypatch):
        existing = {"id": "existing-8", "state": "archived", "content": {"title": "t"}}
        fake = FakeSubstrate([existing])
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = self._file(tmp_path)

        rc = file_task.main([str(spec_path)])

        assert rc == 0
        assert fake.posted is not None

    def test_a_superseded_duplicate_is_not_a_duplicate(self, tmp_path, monkeypatch):
        """A bead already superseded is dead, not merely quiet — re-filing
        against its identity is not a duplicate of live work."""
        existing = {"id": "existing-9b", "state": "superseded", "content": {"title": "t"}}
        fake = FakeSubstrate([existing])
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = self._file(tmp_path)

        rc = file_task.main([str(spec_path)])

        assert rc == 0
        assert fake.posted is not None

    # --- filing is unchanged when nothing matches --------------------------

    def test_no_matching_bead_files_exactly_as_before(self, tmp_path, monkeypatch):
        fake = FakeSubstrate([])
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = self._file(tmp_path)

        rc = file_task.main([str(spec_path)])

        assert rc == 0
        assert fake.posted["content"]["title"] == "t"
        assert fake.posted["content"]["spec_identity"] == file_task._spec_identity(
            str(spec_path)
        )
        assert fake.notes == []

    # --- --supersede ---------------------------------------------------------

    def test_supersede_naming_the_wrong_bead_refuses(self, tmp_path, monkeypatch):
        existing = {"id": "existing-9", "state": "pending", "content": {"title": "t"}}
        fake = FakeSubstrate([existing])
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = self._file(tmp_path)

        with pytest.raises(SystemExit) as exc:
            file_task.main([str(spec_path), "--supersede", "wrong-id"])

        assert "wrong-id" in str(exc.value)
        assert fake.posted is None
        assert fake.notes == []

    def test_supersede_with_nothing_to_supersede_refuses(self, tmp_path, monkeypatch):
        fake = FakeSubstrate([])
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = self._file(tmp_path)

        with pytest.raises(SystemExit) as exc:
            file_task.main([str(spec_path), "--supersede", "nonexistent"])

        assert "nothing to supersede" in str(exc.value)
        assert fake.posted is None
        assert fake.notes == []

    def test_supersede_naming_the_right_bead_proceeds_and_records_both(
        self, tmp_path, monkeypatch
    ):
        existing = {"id": "existing-10", "state": "pending", "content": {"title": "t"}}
        fake = FakeSubstrate([existing])
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = self._file(tmp_path)

        rc = file_task.main([str(spec_path), "--supersede", "existing-10"])

        assert rc == 0
        assert fake.posted is not None
        assert fake.posted["content"]["source_bead_ids"] == ["existing-10"]
        assert len(fake.notes) == 1
        note = fake.notes[0]
        assert note["parent_id"] == "existing-10"
        assert note["kind"] == "status"
        assert "new-bead-id" in note["body"]
        # The state, not just the note, must say the bead is dead — this is
        # the defect the "superseded" state closes: a note-only supersede
        # left the bead readable as "waiting" forever.
        assert fake.transitions == [
            ("existing-10", "pending", "superseded", file_task.CREATED_BY)
        ]
        assert existing["state"] == "superseded"

    def test_supersede_a_failed_bead_transitions_from_failed(
        self, tmp_path, monkeypatch
    ):
        existing = {"id": "existing-11", "state": "failed", "content": {"title": "t"}}
        fake = FakeSubstrate([existing])
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = self._file(tmp_path)

        rc = file_task.main([str(spec_path), "--supersede", "existing-11"])

        assert rc == 0
        assert fake.transitions == [
            ("existing-11", "failed", "superseded", file_task.CREATED_BY)
        ]

    def test_supersede_refuses_a_doing_bead(self, tmp_path, monkeypatch):
        existing = {"id": "existing-12", "state": "doing", "content": {"title": "t"}}
        fake = FakeSubstrate([existing])
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = self._file(tmp_path)

        with pytest.raises(SystemExit) as exc:
            file_task.main([str(spec_path), "--supersede", "existing-12"])

        assert "existing-12" in str(exc.value)
        assert "doing" in str(exc.value)
        assert fake.posted is None
        assert fake.notes == []
        assert fake.transitions == []

    def test_supersede_refuses_a_review_bead(self, tmp_path, monkeypatch):
        existing = {"id": "existing-13", "state": "review", "content": {"title": "t"}}
        fake = FakeSubstrate([existing])
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = self._file(tmp_path)

        with pytest.raises(SystemExit):
            file_task.main([str(spec_path), "--supersede", "existing-13"])

        assert fake.posted is None
        assert fake.notes == []
        assert fake.transitions == []

    def test_supersede_transition_failure_prevents_any_filing(
        self, tmp_path, monkeypatch
    ):
        """OPS-68 reordered --supersede to transition the OLD bead BEFORE the
        new one is created (freeing its identity ahead of the store's own
        uniqueness guarantee, migration 0008). A transition failure now halts
        before anything else happens — no note, no new bead — rather than the
        prior order's note-without-transition inconsistency (the regression
        --supersede originally shipped with)."""
        existing = {"id": "existing-14", "state": "pending", "content": {"title": "t"}}
        fake = FakeSubstrate([existing], transition_error=RuntimeError("409 conflict"))
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = self._file(tmp_path)

        with pytest.raises(SystemExit) as exc:
            file_task.main([str(spec_path), "--supersede", "existing-14"])

        message = str(exc.value)
        assert "existing-14" in message
        assert "Nothing was filed" in message
        assert fake.posted is None
        assert fake.notes == []
        assert fake.transitions == []


# ---------------------------------------------------------------------------
# store-enforced uniqueness under concurrency — OPS-68, 2026-09-07: two
# outer-loop sessions each ran _check_not_a_duplicate, both found nothing
# (the other's create had not landed yet), and both filings succeeded.
# migration 0008 gives the store its own partial unique index on
# content->>'spec_identity' for pending dev.task beads; the create route
# turns a collision into a 409 (routes.py's _is_spec_identity_duplicate_error)
# and file_task.py turns that 409 into the exact same refusal a sequential
# duplicate gets. These tests model the store's guarantee directly (no live
# Postgres) rather than exercising the real HTTP/DB stack.
# ---------------------------------------------------------------------------


class _FakeSpecIdentityConflict(Exception):
    """Stands in for substrate.SubstrateError(409, ...) — file_task.py's
    ``_is_spec_identity_conflict`` duck-types on ``.status``/``.body`` alone,
    so a real SubstrateError is unnecessary here."""

    def __init__(self, spec_identity):
        super().__init__(f"409: spec_identity_duplicate: {spec_identity}")
        self.status = 409
        self.body = json.dumps(
            {"detail": {"error": "spec_identity_duplicate", "spec_identity": spec_identity}}
        )


class FakeSubstrateEnforcingSpecIdentityUniqueness(FakeSubstrate):
    """Models migration 0008's partial unique index server-side: a POST
    /beads for a spec_identity that already has a live 'pending' row in
    ``self._tasks`` is rejected, regardless of what an earlier ``list_tasks``
    call told the caller — exactly like the real constraint, which knows
    nothing about what any reader saw before it fired.

    ``list_tasks`` returns a snapshot frozen at construction time for its
    first ``stale_reads`` calls, then falls back to the live ``self._tasks``.
    That models the incident precisely: a filer's *first* read raced the
    other filer's create and missed it (the frozen snapshot), while a
    *later* re-read (after the store refuses the create) is a fresh query
    against real, current state and correctly sees the winner.
    """

    def __init__(self, tasks, *, stale_reads=1):
        super().__init__(tasks)
        self._frozen_snapshot = list(tasks)
        self._list_calls = 0
        self._stale_reads = stale_reads

    def list_tasks(self, state=None, limit=200):
        self._list_calls += 1
        if self._list_calls <= self._stale_reads:
            return self._frozen_snapshot
        return self._tasks

    def _request(self, method, path, **kwargs):
        if method == "POST" and path == "/beads":
            payload = kwargs.get("json") or {}
            content = payload.get("content") or {}
            identity = content.get("spec_identity")
            if identity and any(
                task.get("state") == "pending"
                and (task.get("content") or {}).get("spec_identity") == identity
                for task in self._tasks
            ):
                raise _FakeSpecIdentityConflict(identity)
            bead = {"id": f"bead-{len(self._tasks)}", "state": "pending", "content": dict(content)}
            self._tasks.append(bead)
            self.posted = payload
            return {"id": bead["id"]}
        return super()._request(method, path, **kwargs)


class TestSpecIdentityConflict:
    def test_is_spec_identity_conflict_detects_the_shape(self):
        assert file_task._is_spec_identity_conflict(_FakeSpecIdentityConflict("x"))

    def test_is_spec_identity_conflict_ignores_other_errors(self):
        assert not file_task._is_spec_identity_conflict(RuntimeError("boom"))
        not_409 = _FakeSpecIdentityConflict("x")
        not_409.status = 500
        assert not file_task._is_spec_identity_conflict(not_409)

    def _file(self, tmp_path, **overrides):
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(_spec(**overrides)))
        return spec_path

    def test_two_concurrent_filings_past_the_read_then_create_window(
        self, tmp_path, monkeypatch
    ):
        """The exact OPS-68 incident: both filers' list_tasks() see nothing
        (neither's create had landed at read time), so the synchronous
        _check_not_a_duplicate check alone lets both through. Only the
        store's own uniqueness guarantee — modeled here by the shared
        backing list both fakes write into — makes the second filer's
        create lose. The loser must get the same refusal message a
        sequential duplicate gets, naming the bead that actually won."""
        shared_tasks: list[dict] = []
        filer_a = FakeSubstrateEnforcingSpecIdentityUniqueness(shared_tasks)
        filer_b = FakeSubstrateEnforcingSpecIdentityUniqueness(shared_tasks)
        spec_path = self._file(tmp_path)

        monkeypatch.setattr(file_task, "Substrate", lambda: filer_a)
        rc = file_task.main([str(spec_path)])
        assert rc == 0
        assert len(shared_tasks) == 1
        winner_id = shared_tasks[0]["id"]

        monkeypatch.setattr(file_task, "Substrate", lambda: filer_b)
        with pytest.raises(SystemExit) as exc:
            file_task.main([str(spec_path)])

        message = str(exc.value)
        assert winner_id in message
        assert "already has a live dev.task" in message
        # Exactly one live bead survives the race.
        assert len(shared_tasks) == 1

    def test_supersede_of_a_pending_bead_survives_the_stores_uniqueness_guarantee(
        self, tmp_path, monkeypatch
    ):
        """A deliberate --supersede of a still-'pending' bead must remain
        unaffected by migration 0008's constraint: OPS-68 orders the old
        bead's transition to 'superseded' before the new create, which is
        exactly what lets this succeed against a store that actually
        enforces the guarantee (rather than the dumb FakeSubstrate that
        never checks it)."""
        existing = {"id": "existing-20", "state": "pending", "content": {"title": "t"}}
        fake = FakeSubstrateEnforcingSpecIdentityUniqueness([existing])
        spec_path = self._file(tmp_path)
        spec_identity = file_task._spec_identity(str(spec_path))
        existing["content"]["spec_identity"] = spec_identity
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)

        rc = file_task.main([str(spec_path), "--supersede", "existing-20"])

        assert rc == 0
        assert existing["state"] == "superseded"
        assert fake.posted is not None
        assert fake.notes[0]["parent_id"] == "existing-20"


# ---------------------------------------------------------------------------
# --supersede repoints dependents — OPS-13, 2026-08-24: re-filing S33-B2 with
# --supersede replaced f9fc8ff9 with 658a1d8a and recorded the supersession
# correctly on both beads, but four dependents still named f9fc8ff9 in
# predecessor_bead_ids: each was reported "predecessor is held (superseded)"
# by is_runnable, forever, until they were repointed by hand.
# ---------------------------------------------------------------------------


class TestSupersedeRepointsDependents:
    def _file(self, tmp_path, **overrides):
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(_spec(**overrides)))
        return spec_path

    def _dependent(self, dep_id, *predecessor_ids):
        return {
            "id": dep_id,
            "state": "pending",
            "content": {
                "title": "dependent",
                "predecessor_bead_ids": list(predecessor_ids),
            },
        }

    def test_supersede_repoints_every_dependent_at_the_replacement(
        self, tmp_path, monkeypatch
    ):
        """The TDD test: fails against today's behaviour, naming both
        stranded dependents in the assertion that catches it."""
        existing = {"id": "existing-15", "state": "pending", "content": {"title": "t"}}
        dep_a = self._dependent("dep-a", "existing-15")
        dep_b = self._dependent("dep-b", "existing-15")
        fake = FakeSubstrate([existing, dep_a, dep_b])
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = self._file(tmp_path)

        rc = file_task.main([str(spec_path), "--supersede", "existing-15"])

        assert rc == 0
        stranded = [
            dep["id"]
            for dep in (dep_a, dep_b)
            if "existing-15" in dep["content"]["predecessor_bead_ids"]
        ]
        assert stranded == [], f"still pointing at the dead bead: {stranded}"
        assert dep_a["content"]["predecessor_bead_ids"] == ["new-bead-id"]
        assert dep_b["content"]["predecessor_bead_ids"] == ["new-bead-id"]

    def test_supersede_repoint_leaves_unrelated_predecessors_untouched(
        self, tmp_path, monkeypatch
    ):
        existing = {"id": "existing-16", "state": "pending", "content": {"title": "t"}}
        dep = self._dependent("dep-c", "other-bead", "existing-16")
        fake = FakeSubstrate([existing, dep])
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = self._file(tmp_path)

        rc = file_task.main([str(spec_path), "--supersede", "existing-16"])

        assert rc == 0
        assert dep["content"]["predecessor_bead_ids"] == ["other-bead", "new-bead-id"]

    def test_supersede_repoint_does_not_duplicate_an_already_named_replacement(
        self, tmp_path, monkeypatch
    ):
        existing = {"id": "existing-17", "state": "pending", "content": {"title": "t"}}
        # Already names both the soon-to-be-superseded bead and the id the
        # new filing will receive ("new-bead-id", per FakeSubstrate._request).
        dep = self._dependent("dep-d", "existing-17", "new-bead-id")
        fake = FakeSubstrate([existing, dep])
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = self._file(tmp_path)

        rc = file_task.main([str(spec_path), "--supersede", "existing-17"])

        assert rc == 0
        assert dep["content"]["predecessor_bead_ids"] == ["new-bead-id"]

    def test_supersede_with_no_dependents_repoints_nothing(self, tmp_path, monkeypatch):
        existing = {"id": "existing-18", "state": "pending", "content": {"title": "t"}}
        fake = FakeSubstrate([existing])
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = self._file(tmp_path)

        rc = file_task.main([str(spec_path), "--supersede", "existing-18"])

        assert rc == 0
        assert fake.patches == []

    def test_supersede_reports_a_repoint_failure_loudly_instead_of_a_partial_silent_state(
        self, tmp_path, monkeypatch
    ):
        existing = {"id": "existing-19", "state": "pending", "content": {"title": "t"}}
        dep = self._dependent("dep-e", "existing-19")
        fake = FakeSubstrate([existing, dep], patch_error_for={"dep-e"})
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = self._file(tmp_path)

        with pytest.raises(SystemExit) as exc:
            file_task.main([str(spec_path), "--supersede", "existing-19"])

        message = str(exc.value)
        assert "dep-e" in message
        # The supersession itself must still have landed — a failed repoint
        # is reported as its own problem, not allowed to undo or hide the
        # (already-succeeded) supersession.
        assert fake.transitions == [
            ("existing-19", "pending", "superseded", file_task.CREATED_BY)
        ]


# ---------------------------------------------------------------------------
# OPS-107-shaped — a predecessor that is ALSO this filing's own supersession
# target: "must land first" and "is being replaced" contradict for the same
# bead. This guards a shape, not a replay of OPS-107's history: bead 70b01c44
# named 79a6fba7 in predecessor_bead_ids while it was still pending, and
# 79a6fba7 was then superseded BY HAND roughly 39 s after filing, outside this
# script, with source_bead_ids=[] -- the gap _check_predecessor_supersession
# closes by reading the predecessor's own state. The twelve hours OPS-107 lost
# sitting unclaimed happened entirely after filing succeeded.
# ---------------------------------------------------------------------------


class TestPredecessorSupersessionContradiction:
    def test_refuses_when_predecessor_is_the_supersede_target(self):
        superseded_task = {"id": "79a6fba7", "state": "pending", "content": {"title": "t"}}

        with pytest.raises(SystemExit) as exc:
            file_task._check_not_own_supersession_target(["79a6fba7"], superseded_task)

        assert "79a6fba7" in str(exc.value)
        assert "--supersede" in str(exc.value)

    def test_allows_a_predecessor_that_is_not_the_supersede_target(self):
        superseded_task = {"id": "other-bead", "state": "pending", "content": {"title": "t"}}

        file_task._check_not_own_supersession_target(["79a6fba7"], superseded_task)

    def test_allows_predecessors_when_nothing_is_being_superseded(self):
        file_task._check_not_own_supersession_target(["79a6fba7"], None)

    def test_main_reproduces_the_ops_107_shape(self, tmp_path, monkeypatch):
        """The OPS-107 SHAPE, not its history: predecessor_bead_ids names the
        bead this same filing supersedes via --supersede, because the spec's
        identity matched an open duplicate that also happened to be the bead
        it depends on. (The real OPS-107 predecessor was superseded by hand,
        after filing; see the section header above.)"""
        existing = {"id": "79a6fba7", "state": "pending", "content": {"title": "t"}}
        fake = FakeSubstrate([existing])
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(_spec(predecessor_bead_ids=["79a6fba7"])))

        with pytest.raises(SystemExit) as exc:
            file_task.main([str(spec_path), "--supersede", "79a6fba7"])

        assert "79a6fba7" in str(exc.value)
        assert fake.posted is None
        assert fake.notes == []
        assert fake.transitions == []


# ---------------------------------------------------------------------------
# source_bead_ids is written by two paths and only one was ever validated.
# --supersede runs three checks (identity match, legal-source-state,
# predecessor contradiction) before it lands a bead id in
# content.source_bead_ids. build_content (file_task.py:678) copies a spec's
# own source_bead_ids straight through with none of them. guards.
# superseding_task_ids reverse-indexes the board on that field alone, so a
# spec naming a live dev.task there makes guards.is_runnable refuse it and
# dispatch.py --requeue report reason=superseded_by, with nothing on the
# bead saying why. Measured 2026-09-16: a failed, unrequeueable bead
# (9defb4c5) and the exact contradiction _check_not_own_supersession_target
# exists to catch (d643b5ab named adfa90a0 in BOTH predecessor_bead_ids and
# source_bead_ids) — that guard is only ever reached from the --supersede
# resolution path, so it never fired.
# ---------------------------------------------------------------------------


class TestSourceBeadIdsNotLiveTasks:
    def test_refuses_a_live_pending_task(self):
        task = {"id": "9defb4c5", "state": "pending", "content": {"title": "t"}}

        with pytest.raises(SystemExit) as exc:
            file_task._check_source_bead_ids_not_live_tasks(
                ["9defb4c5"], [], [task]
            )

        assert "9defb4c5" in str(exc.value)
        assert "--supersede" in str(exc.value)

    def test_refuses_a_failed_task(self):
        """9defb4c5's actual shape: failed and falsely superseded, which
        strands it — dispatch.py --requeue refuses a superseded bead, so a
        failed-and-superseded bead can neither land nor be retried."""
        task = {"id": "9defb4c5", "state": "failed", "content": {"title": "t"}}

        with pytest.raises(SystemExit) as exc:
            file_task._check_source_bead_ids_not_live_tasks(
                ["9defb4c5"], [], [task]
            )

        assert "9defb4c5" in str(exc.value)
        assert "failed" in str(exc.value)

    def test_refuses_a_review_task_with_an_open_pr(self):
        """adfa90a0/6aa27a4f's shape: review state, open merge-ready PR."""
        task = {"id": "adfa90a0", "state": "review", "content": {"title": "t"}}

        with pytest.raises(SystemExit) as exc:
            file_task._check_source_bead_ids_not_live_tasks(
                ["adfa90a0"], [], [task]
            )

        assert "adfa90a0" in str(exc.value)
        assert "review" in str(exc.value)

    def test_refuses_a_doing_task(self):
        task = {"id": "x1", "state": "doing", "content": {"title": "t"}}

        with pytest.raises(SystemExit) as exc:
            file_task._check_source_bead_ids_not_live_tasks(["x1"], [], [task])

        assert "x1" in str(exc.value)

    def test_names_the_contradiction_when_also_a_predecessor(self):
        """The exact d643b5ab shape: adfa90a0 named in BOTH
        predecessor_bead_ids and source_bead_ids of the same filing."""
        task = {"id": "adfa90a0", "state": "review", "content": {"title": "t"}}

        with pytest.raises(SystemExit) as exc:
            file_task._check_source_bead_ids_not_live_tasks(
                ["adfa90a0"], ["adfa90a0"], [task]
            )

        assert "adfa90a0" in str(exc.value)
        assert "predecessor_bead_ids" in str(exc.value)

    def test_names_the_alternative_carrier(self):
        task = {"id": "x1", "state": "pending", "content": {"title": "t"}}

        with pytest.raises(SystemExit) as exc:
            file_task._check_source_bead_ids_not_live_tasks(["x1"], [], [task])

        message = str(exc.value)
        assert "derived_from" in message
        assert "found_by" in message

    def test_allows_a_done_task(self):
        task = {"id": "d1", "state": "done", "content": {"title": "t"}}
        file_task._check_source_bead_ids_not_live_tasks(["d1"], [], [task])

    def test_allows_an_archived_task(self):
        task = {"id": "a1", "state": "archived", "content": {"title": "t"}}
        file_task._check_source_bead_ids_not_live_tasks(["a1"], [], [task])

    def test_allows_an_already_superseded_task(self):
        task = {"id": "s1", "state": "superseded", "content": {"title": "t"}}
        file_task._check_source_bead_ids_not_live_tasks(["s1"], [], [task])

    def test_allows_an_id_that_names_no_dev_task(self):
        """A dev.finding id (or anything else sub.list_tasks() never
        returns) never collides with this check — non-task provenance
        stays expressible with no special-casing."""
        task = {"id": "9defb4c5", "state": "pending", "content": {"title": "t"}}
        file_task._check_source_bead_ids_not_live_tasks(
            ["some-dev-finding-id"], [], [task]
        )

    def test_allows_an_empty_list(self):
        file_task._check_source_bead_ids_not_live_tasks([], [], [])

    def test_allows_a_live_task_that_matches_supersede(self):
        """The one narrowing this bead adds: a source_bead_ids entry that
        names the SAME id as --supersede is not a silent, unaudited write --
        it is what --supersede is already about to make true."""
        task = {"id": "existing-30", "state": "pending", "content": {"title": "t"}}
        file_task._check_source_bead_ids_not_live_tasks(
            ["existing-30"], [], [task], supersede="existing-30"
        )

    def test_still_refuses_a_different_live_task_when_supersede_given(self):
        """The exemption is for the matching id only -- a different live
        dev.task named in source_bead_ids is still refused even though
        --supersede names something else on the same filing."""
        task = {"id": "other-live", "state": "pending", "content": {"title": "t"}}

        with pytest.raises(SystemExit) as exc:
            file_task._check_source_bead_ids_not_live_tasks(
                ["other-live"], [], [task], supersede="existing-30"
            )

        assert "other-live" in str(exc.value)

    def test_main_refuses_the_d643b5ab_contradiction_shape(self, tmp_path, monkeypatch):
        """End-to-end: the exact spec shape that filed cleanly on 2026-09-16
        must now be refused before any bead is created."""
        existing = {"id": "adfa90a0", "state": "review", "content": {"title": "unrelated"}}
        fake = FakeSubstrate([existing])
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(
            json.dumps(
                _spec(
                    predecessor_bead_ids=["adfa90a0"],
                    source_bead_ids=["adfa90a0"],
                )
            )
        )

        with pytest.raises(SystemExit) as exc:
            file_task.main([str(spec_path)])

        assert "adfa90a0" in str(exc.value)
        assert fake.posted is None

    def test_main_refuses_a_live_source_bead_id_without_supersede(
        self, tmp_path, monkeypatch
    ):
        """A spec that hand-writes a live dev.task id into source_bead_ids,
        with no --supersede at all, must still be refused rather than
        silently minting a false supersession."""
        existing = {"id": "9defb4c5", "state": "failed", "content": {"title": "unrelated"}}
        fake = FakeSubstrate([existing])
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(_spec(source_bead_ids=["9defb4c5"])))

        with pytest.raises(SystemExit) as exc:
            file_task.main([str(spec_path)])

        assert "9defb4c5" in str(exc.value)
        assert fake.posted is None

    def test_main_still_files_a_source_bead_id_naming_a_closed_task(
        self, tmp_path, monkeypatch
    ):
        existing = {"id": "d1", "state": "done", "content": {"title": "unrelated"}}
        fake = FakeSubstrate([existing])
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(_spec(source_bead_ids=["d1"])))

        rc = file_task.main([str(spec_path)])

        assert rc == 0
        assert fake.posted["content"]["source_bead_ids"] == ["d1"]

    def test_main_supersede_path_is_unaffected(self, tmp_path, monkeypatch):
        """--supersede itself must keep working exactly as before: the
        replacement id it writes into source_bead_ids is the bead it just
        validated and transitioned, not a hand-written spec value."""
        existing = {"id": "existing-30", "state": "pending", "content": {"title": "t"}}
        fake = FakeSubstrate([existing])
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(_spec()))

        rc = file_task.main([str(spec_path), "--supersede", "existing-30"])

        assert rc == 0
        assert fake.posted["content"]["source_bead_ids"] == ["existing-30"]
        assert existing["state"] == "superseded"

    def test_main_allows_source_bead_ids_matching_supersede(self, tmp_path, monkeypatch):
        """THE KNOWN-BAD CONTROL (release gate PR #890, finding 1): naming
        the same bead in source_bead_ids AND --supersede must be allowed, and
        the resulting bead's source_bead_ids must contain it exactly once --
        not twice, which is what the append at file_spec's tail would
        produce if this exemption didn't already sit in source_bead_ids."""
        existing = {"id": "existing-30", "state": "pending", "content": {"title": "t"}}
        fake = FakeSubstrate([existing])
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(_spec(source_bead_ids=["existing-30"])))

        rc = file_task.main([str(spec_path), "--supersede", "existing-30"])

        assert rc == 0
        assert fake.posted["content"]["source_bead_ids"] == ["existing-30"]
        assert existing["state"] == "superseded"

    def test_main_still_refuses_a_different_live_source_bead_id_than_supersede(
        self, tmp_path, monkeypatch
    ):
        """The exemption is for the matching id only, never a blanket pass
        whenever --supersede is present: a spec naming a DIFFERENT live
        dev.task in source_bead_ids than the one --supersede names must
        still be refused."""
        superseded = {"id": "existing-30", "state": "pending", "content": {"title": "t"}}
        other_live = {
            "id": "other-live",
            "state": "pending",
            "content": {"title": "unrelated"},
        }
        fake = FakeSubstrate([superseded, other_live])
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(_spec(source_bead_ids=["other-live"])))

        with pytest.raises(SystemExit) as exc:
            file_task.main([str(spec_path), "--supersede", "existing-30"])

        assert "other-live" in str(exc.value)
        assert fake.posted is None


# ---------------------------------------------------------------------------
# worker_hint — B-FRESH-1's spec carried worker_hint='codex' after the Operator
# retired codex 2026-08-22 (OPS-4); nothing rejected it at filing, and
# dispatch.select_worker's fallback (silently running the default worker
# instead) only surfaces the mistake after a run has already happened.
# ---------------------------------------------------------------------------


class TestWorkerHint:
    def test_refuses_an_unknown_worker_hint(self):
        with pytest.raises(SystemExit) as exc:
            file_task._check_worker_hint({"worker_hint": "not-a-real-worker"})

        assert "not-a-real-worker" in str(exc.value)

    def test_refuses_a_retired_worker_hint(self, monkeypatch):
        monkeypatch.setitem(
            dispatch.WORKER_REGISTRY,
            "oldtool",
            dispatch.WorkerEntry(
                argv=("oldtool",),
                quarantined=False,
                allowed_lanes=dispatch.DEV_LANES,
                retired=True,
                retirement_reason="Amendment 30",
            ),
        )
        with pytest.raises(SystemExit) as exc:
            file_task._check_worker_hint({"worker_hint": "oldtool"})

        message = str(exc.value)
        assert "oldtool" in message
        assert "retired" in message

    def test_allows_a_live_worker_hint(self):
        file_task._check_worker_hint({"worker_hint": "claude"})

    def test_allows_no_hint_at_all(self):
        file_task._check_worker_hint({})

    def test_build_content_refuses_a_retired_worker_hint(self, monkeypatch):
        monkeypatch.setitem(
            dispatch.WORKER_REGISTRY,
            "oldtool",
            dispatch.WorkerEntry(
                argv=("oldtool",),
                quarantined=False,
                allowed_lanes=dispatch.DEV_LANES,
                retired=True,
                retirement_reason="Amendment 30",
            ),
        )
        with pytest.raises(SystemExit) as exc:
            file_task.build_content(_spec(worker_hint="oldtool"))

        assert "oldtool" in str(exc.value)

    def test_build_content_survives_a_live_worker_hint(self):
        content = file_task.build_content(_spec(worker_hint="claude"))

        assert content["worker_hint"] == "claude"


# ---------------------------------------------------------------------------
# structural risk_class vs. a behaviour-mandating acceptance criterion —
# R2605-5's AC-4 required a duplicate-registration REFUSAL in a bead whose own
# risk_class was 'structural', unsatisfiable under Tidy-First. This is prose
# classification, so it warns and never blocks (a false positive here is
# worse than the defect it catches).
# ---------------------------------------------------------------------------


class TestStructuralBehaviorWarnings:
    def test_warns_on_the_r2605_5_ac4_shape(self):
        content = {
            "risk_class": "structural",
            "acceptance": [
                "WHEN a duplicate registration is attempted, THE service SHALL "
                "refuse it with a 409."
            ],
        }

        warnings = file_task.structural_behavior_warnings(content)

        assert warnings == [
            "WHEN a duplicate registration is attempted, THE service SHALL "
            "refuse it with a 409."
        ]

    def test_does_not_warn_on_a_legitimate_structural_criterion(self):
        content = {
            "risk_class": "structural",
            "acceptance": [
                "THE existing test suite SHALL pass unmodified after the move.",
                "THE extracted module SHALL produce byte-identical output.",
            ],
        }

        assert file_task.structural_behavior_warnings(content) == []

    def test_does_not_warn_on_a_behavioral_bead(self):
        content = {
            "risk_class": "behavioral",
            "acceptance": ["THE service SHALL refuse a duplicate registration."],
        }

        assert file_task.structural_behavior_warnings(content) == []

    def test_shall_not_phrasing_does_not_warn(self):
        content = {
            "risk_class": "structural",
            "acceptance": ["THE move SHALL NOT reject any input the original accepted."],
        }

        assert file_task.structural_behavior_warnings(content) == []

    def test_main_prints_but_does_not_refuse(self, tmp_path, monkeypatch, capsys):
        fake = FakeSubstrate([])
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(
            json.dumps(
                _spec(
                    risk_class="structural",
                    acceptance=["THE service SHALL refuse a duplicate registration."],
                )
            )
        )

        rc = file_task.main([str(spec_path)])

        assert rc == 0
        assert fake.posted is not None
        assert "appears to mandate new behaviour" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# live-store access (D7) — dev.task ad0a0f95's AC-1/AC-3/AC-5 directed the
# worker at the live production store, and AC-1 required mutating it; the
# worker complied, rewriting 1019 production arch beads to source_class
# 'derived' forty-six minutes before the commit implementing that code was
# even authored (dev.finding 9c9ccd9f, MEASURED 2026-09-16). D7 (CLAUDE.md)
# already said a spec needing store-shaped evidence must cite a committed
# fixture/snapshot, or declare that the outer loop runs the live comparison
# itself at the gate -- but that rule lived only in CLAUDE.md, carried by the
# outer loop remembering it at filing time. This is the mechanical carrier.
#
# A hand-run naive keyword match over every pending/doing bead (2026-09-16)
# over-fired on four real, already-filed specs that use this exact
# vocabulary to PROHIBIT live-store access, not direct it. Their text is
# reproduced verbatim below as the driving false-positive controls.
# ---------------------------------------------------------------------------


class TestLiveStoreAccess:
    # --- the false-positive controls (measured 2026-09-16) -----------------

    def test_ops_134_no_live_store_needed_does_not_refuse(self):
        """adfa90a0 (OPS-134): 'supplied here so no live store, no GitHub
        API call and no network access is needed' -- a prohibition, not a
        direction; naive keyword matching over-fired on it."""
        file_task._check_live_store_access(_spec(
            acceptance=[
                "The fixture data SHALL be supplied here so no live store, "
                "no GitHub API call and no network access is needed."
            ],
        ))

    def test_ops_139_shall_not_read_or_write_does_not_refuse(self):
        """95b909ca (OPS-139): 'THE WORKER SHALL NOT READ OR WRITE THE LIVE
        STORE to satisfy any criterion here.'"""
        file_task._check_live_store_access(_spec(
            acceptance=[
                "THE WORKER SHALL NOT READ OR WRITE THE LIVE STORE to "
                "satisfy any criterion here."
            ],
        ))

    def test_ops_140_not_by_a_fresh_live_read_does_not_refuse(self):
        """a93a348d (OPS-140): 'measured against the store read recorded in
        dev.finding 7142bc58, not by a fresh live read.'"""
        file_task._check_live_store_access(_spec(
            acceptance=[
                "THE count SHALL be measured against the store read "
                "recorded in dev.finding 7142bc58, not by a fresh live read."
            ],
        ))

    def test_this_bead_do_not_add_a_fixture_against_prod_does_not_refuse(self):
        """9849698b (this bead): 'do not add a fixture that resolves
        against prod.'"""
        file_task._check_live_store_access(_spec(
            acceptance=[
                "If a test needs a spec, build it inline or commit it under "
                "the dispatcher's test tree; do not add a fixture that "
                "resolves against prod."
            ],
        ))

    def test_this_bead_full_spec_needs_the_declaration(self):
        """9849698b's own committed acceptance text, verbatim (the fourth
        AC-6 control), driven through the real check rather than one
        hand-picked sentence -- release gate F1 on PR #883, head 9c43521c.

        Unlike the other three controls, this one does NOT file clean on
        heuristic tuning alone: acceptance[0] DESCRIBES the check ("FILING
        SHALL REFUSE A SPEC WHOSE ... TEXT POINTS AT THE LIVE STORE",
        "Detect the shape -- references to the live/production store,
        prod, ...") and acceptance[2] QUOTES ad0a0f95's own violating AC-2
        text as a worked example. A description of a prohibition and the
        prohibition's own direction are, again, the same words -- there is
        no tuning of _LIVE_STORE_KEYWORD_RE/_NEGATION_WORD_RE that
        distinguishes "this check refuses text that names a script and a
        run directive" from actually naming a script and a run directive,
        because they ARE the same text. This is the case F1 named as the
        fallback: a spec describing the check declares ``live_store_access``
        rather than being made to pass by widening the heuristic to fit one
        more example.
        """
        acceptance = [
            "FILING SHALL REFUSE A SPEC WHOSE ACCEPTANCE OR VERIFICATION "
            "TEXT POINTS AT THE LIVE STORE WITHOUT AN EXPLICIT DECLARATION. "
            "Detect the shape -- references to the live/production store, "
            "prod, the substrate URL or a live read/write -- in acceptance, "
            "verification and intent, and refuse with a message that names "
            "D7, quotes the offending text, and states the two ways to "
            "satisfy it: cite a committed fixture or snapshot the outer "
            "loop supplies, or declare that the outer loop runs the "
            "comparison at the gate. The refusal SHALL be a refusal, not a "
            "warning: file_task.py's existing refusals exit non-zero and "
            "this SHALL too.",
            "AN EXPLICIT DECLARATION SHALL SATISFY IT. Add a content field "
            "a filer sets deliberately -- for example live_store_access "
            "with a stated reason -- and a spec carrying it SHALL file "
            "successfully with the same text that would otherwise refuse. "
            "Verify both directions with literal spec fixtures: the same "
            "acceptance text refuses without the field and files with it. "
            "A check with no escape hatch gets routed around by rewording, "
            "which is worse than no check.",
            "THE RATCHET SHALL FIRE ON ad0a0f95's REAL ACCEPTANCE TEXT. "
            "The two ACs that caused the incident, verbatim: AC-2: 'THE "
            "backfill SHALL be run through scripts/arch-source-class-"
            "backfill.py rather than a new one-off script, and this bead "
            "SHALL NOT weaken that script's stated safety properties...' "
            "AC-5: 'THE COUNT SHALL BE RE-MEASURED AT EXECUTION, NOT "
            "INHERITED FROM THIS SPEC.' Neither contains 'live store', "
            "'prod store' or 'against prod'. The detector MUST refuse a "
            "spec carrying these two criteria.",
            "EVERY SPEC ALREADY IN apps/factory-dispatcher/tasks/ SHALL "
            "STILL FILE, OR ITS REFUSAL SHALL BE DELIBERATE. Run the new "
            "check over all committed specs and report, in the PR body, "
            "the full list it would refuse. THE WORKER SHALL NOT EDIT "
            "THOSE SPECS TO MAKE THEM PASS.",
            "NO NETWORK, NO STORE, IN ANY TEST. The check is textual and "
            "every fixture is literal; do not add a fixture that resolves "
            "against prod.",
            "THE FALSE-POSITIVE DIRECTION SHALL BE DRIVEN. A naive keyword "
            "match over-fires on specs that use this vocabulary to "
            "PROHIBIT live-store access rather than direct it: 'no live "
            "store ... is needed' (OPS-134), 'THE WORKER SHALL NOT READ OR "
            "WRITE THE LIVE STORE' (OPS-139), 'not by a fresh live read' "
            "(OPS-140).",
            "THE EXISTING SUITES SHALL STAY GREEN.",
        ]

        with pytest.raises(SystemExit) as exc:
            file_task._check_live_store_access(_spec(acceptance=acceptance))
        assert "live_store_access" in str(exc.value)

        file_task._check_live_store_access(_spec(
            acceptance=acceptance,
            live_store_access=(
                "this spec describes and quotes the D7 live-store-access "
                "check itself; it does not direct a worker to read or "
                "write the live store"
            ),
        ))

    # --- the real historical case --------------------------------------
    #
    # dev.task ad0a0f95's REAL AC-2 and AC-5 text, verbatim, supplied by the
    # outer loop on the requeue (release gate F4 on PR #883, head 9c43521c:
    # the earlier fixture was a paraphrase reconstructing the shape, and
    # could not falsify the detector it was written for). Neither contains
    # "live store", "prod store" or "against prod" -- #883's keyword-only
    # design scored zero hits on both, which is why this control did not
    # exist before: AC-2 names a substrate-writing script and directs the
    # worker to run it; AC-5 demands a count be re-measured at execution
    # rather than inherited from a committed snapshot.

    def test_the_ad0a0f95_real_ac2_and_ac5_shape_refuses(self):
        spec = _spec(acceptance=[
            "THE backfill SHALL be run through "
            "scripts/arch-source-class-backfill.py rather than a new "
            "one-off script, and this bead SHALL NOT weaken that script's "
            "stated safety properties...",
            "THE COUNT SHALL BE RE-MEASURED AT EXECUTION, NOT INHERITED "
            "FROM THIS SPEC. 920/911 is a dated snapshot from 2026-09-14 "
            "and the nightly writers add rows continuously; the PR body "
            "SHALL state the count it actually saw and the date it saw it.",
        ])

        with pytest.raises(SystemExit) as exc:
            file_task._check_live_store_access(spec)

        message = str(exc.value)
        assert "live/production store" in message
        assert "D7" in message

    def test_the_ad0a0f95_ac2_alone_refuses_on_the_script_run_directive(self):
        with pytest.raises(SystemExit):
            file_task._check_live_store_access(_spec(acceptance=[
                "THE backfill SHALL be run through "
                "scripts/arch-source-class-backfill.py rather than a new "
                "one-off script.",
            ]))

    def test_the_ad0a0f95_ac5_alone_refuses_on_the_execution_time_measurement(self):
        with pytest.raises(SystemExit):
            file_task._check_live_store_access(_spec(acceptance=[
                "THE COUNT SHALL BE RE-MEASURED AT EXECUTION, NOT INHERITED "
                "FROM THIS SPEC.",
            ]))

    def test_naming_a_script_without_a_run_directive_does_not_refuse(self):
        """M7 (committed, apps/factory-dispatcher/tasks/): names the same
        script ad0a0f95's AC-2 named, but only to say it SHALL drop its own
        HTTP client -- a refactor, not a direction to run it. The script-run
        signal keys on 'SHALL be run through', not on the bare filename, so
        this must not become a new false positive."""
        file_task._check_live_store_access(_spec(acceptance=[
            "arch-source-class-backfill.py, ea-derive.py, "
            "regression-test.py and principles_sync.py SHALL drop their "
            "own HTTP and use substrate_client; every importer of their "
            "removed helpers SHALL be re-pointed in the same PR.",
        ]))

    # --- F1 (release gate on PR #888): the structural signal must catch
    # natural paraphrases of AC-2's exact wording, not just the literal
    # "SHALL be run through" phrase the old keyword-only design was fitted
    # to. Re-derived by the outer loop: eight of nine paraphrases of #883's
    # two literal patterns slipped through. These four are that finding's
    # own paraphrase set, now driven through the structural check.

    def test_paraphrase_shall_be_executed_using_refuses(self):
        with pytest.raises(SystemExit):
            file_task._check_live_store_access(_spec(acceptance=[
                "THE backfill SHALL be executed using "
                "scripts/arch-source-class-backfill.py.",
            ]))

    def test_paraphrase_the_worker_shall_run_refuses(self):
        with pytest.raises(SystemExit):
            file_task._check_live_store_access(_spec(acceptance=[
                "THE worker SHALL run "
                "scripts/arch-source-class-backfill.py to perform the "
                "backfill.",
            ]))

    def test_paraphrase_shall_be_performed_by_refuses(self):
        with pytest.raises(SystemExit):
            file_task._check_live_store_access(_spec(acceptance=[
                "THE backfill SHALL be performed by "
                "scripts/arch-source-class-backfill.py.",
            ]))

    def test_paraphrase_shall_be_run_using_refuses(self):
        with pytest.raises(SystemExit):
            file_task._check_live_store_access(_spec(acceptance=[
                "THE backfill SHALL be run using "
                "scripts/arch-source-class-backfill.py.",
            ]))

    def test_substrate_reaching_script_names_is_computed_not_hardcoded(self):
        """The structural signal's script list comes from reading
        scripts/*.py at check time (AC-1's own instruction: 'which committed
        scripts write to the substrate is knowable from the repository'),
        not from a literal name copied into this file."""
        names = file_task._substrate_reaching_script_names()
        assert "arch-source-class-backfill.py" in names
        assert "ea-derive.py" in names
        # A script this repo carries that never touches the substrate at all
        # (this test file's own directory has none importable here, so the
        # negative check is simply that the set is a strict subset of what
        # scripts/ contains, i.e. it is filtering something).
        assert names < {p.name for p in file_task.SCRIPTS_DIR.glob("*.py")}

    def test_build_content_refuses_the_ad0a0f95_shape(self):
        with pytest.raises(SystemExit) as exc:
            file_task.build_content(_spec(acceptance=[
                "THE backfill SHALL be run through "
                "scripts/arch-source-class-backfill.py rather than a new "
                "one-off script.",
            ]))

        assert "live/production store" in str(exc.value)

    # --- the shape is refused without an escape hatch -----------------------

    def test_refuses_a_bare_live_store_direction(self):
        with pytest.raises(SystemExit) as exc:
            file_task._check_live_store_access(_spec(
                acceptance=["THE worker SHALL read the live production store."],
            ))

        message = str(exc.value)
        assert "D7" in message
        assert "live production store" in message
        assert "live_store_access" in message

    def test_refuses_on_verification_commands(self):
        with pytest.raises(SystemExit) as exc:
            file_task._check_live_store_access(_spec(
                verification={"commands": ["curl $SUBSTRATE_URL/beads | jq ."]},
            ))

        assert "verification.commands[0]" in str(exc.value)

    def test_refuses_on_intent(self):
        with pytest.raises(SystemExit) as exc:
            file_task._check_live_store_access(_spec(
                intent="Measured live against the prod substrate: 12 beads.",
            ))

        assert "intent" in str(exc.value)

    def test_quotes_the_offending_clause(self):
        with pytest.raises(SystemExit) as exc:
            file_task._check_live_store_access(_spec(
                acceptance=["THE worker SHALL read the live production store."],
            ))

        assert "THE worker SHALL read the live production store." in str(exc.value)

    # --- the escape hatch: live_store_access ------------------------------

    def test_live_store_access_declaration_satisfies_it(self):
        """The exact text that refuses above files clean once the filer
        declares why -- a check with no escape hatch gets routed around by
        rewording, which is worse than no check."""
        file_task._check_live_store_access(_spec(
            acceptance=["THE worker SHALL read the live production store."],
            live_store_access="outer loop runs this comparison at the gate, not the worker",
        ))

    def test_build_content_files_with_the_declaration_and_keeps_it(self):
        content = file_task.build_content(_spec(
            acceptance=["THE worker SHALL read the live production store."],
            live_store_access="outer loop runs this comparison at the gate, not the worker",
        ))

        assert content["live_store_access"] == (
            "outer loop runs this comparison at the gate, not the worker"
        )

    def test_blank_declaration_does_not_count(self):
        with pytest.raises(SystemExit):
            file_task._check_live_store_access(_spec(
                acceptance=["THE worker SHALL read the live production store."],
                live_store_access="   ",
            ))

    def test_absent_declaration_is_not_written(self):
        content = file_task.build_content(_spec())
        assert "live_store_access" not in content

    # --- ordinary specs are unaffected --------------------------------------

    def test_a_spec_with_no_live_store_shape_files_clean(self):
        file_task._check_live_store_access(_spec())

    def test_the_word_production_alone_does_not_match(self):
        """'production' without 'store' (e.g. an incident, a release) is not
        the shape D7 names -- only 'production store' is."""
        file_task._check_live_store_access(_spec(
            acceptance=["THE fix SHALL prevent the production incident from recurring."],
        ))

    def test_the_word_product_does_not_match_prod(self):
        file_task._check_live_store_access(_spec(
            acceptance=["THE product owner SHALL review the release notes."],
        ))

    # --- F3: bare `prod` requires an adjacent store noun --------------------
    # Release gate on PR #883 (head 9c43521c): bare `prod` fired on prose
    # that only names production without directing anyone at it. Each of
    # these is real committed corpus text that used to refuse and must not
    # once `prod` requires `store`/`substrate` next to it.

    def test_prod_as_a_bare_symptom_does_not_refuse(self):
        """OPS-110: 'is inert in prod.' -- a symptom, not a direction."""
        file_task._check_live_store_access(_spec(
            acceptance=["A second language is inert in prod."],
        ))

    def test_prod_inside_a_namespace_name_does_not_refuse(self):
        """OPS-109/M4: 'platform-mcp-prod'/'mcp-hub-prod' are k8s namespace
        names, not a direction at the live store."""
        file_task._check_live_store_access(_spec(
            acceptance=[
                "After deploy, `temporal schedule list --namespace "
                "mcp-hub-prod` SHALL no longer list goose-dispatch."
            ],
        ))

    def test_prod_narrating_a_past_measurement_does_not_refuse(self):
        """OPS-122: '#804's worker measured its AC-4 numbers against prod
        from inside its clone' narrates a past incident; it is not this
        spec directing a worker at the store."""
        file_task._check_live_store_access(_spec(
            acceptance=[
                "2026-09-12: #804's worker measured its AC-4 numbers "
                "against prod from inside its clone. Recorded as "
                "dev.finding c48a3827."
            ],
        ))

    def test_prod_store_still_refuses(self):
        """`prod` adjacent to a store noun is exactly the shape D7 names
        and must still refuse -- the tightened regex narrows what counts
        as adjacent, it does not drop the signal."""
        with pytest.raises(SystemExit):
            file_task._check_live_store_access(_spec(
                acceptance=["THE worker SHALL read the prod store directly."],
            ))

    def test_prod_substrate_still_refuses(self):
        with pytest.raises(SystemExit):
            file_task._check_live_store_access(_spec(
                acceptance=["Measured live against the prod substrate: 12 beads."],
            ))

    # --- F2: nothing/none/without/neither/nor are negation cues -------------
    # \bnot\b and \bno\b are word-bounded and never matched inside "Nothing"
    # or "None" -- the most natural English phrasing of the D7 prohibition
    # was invisible to the check.

    def test_nothing_negates_like_not(self):
        """OPS-121 acceptance[3], committed verbatim."""
        file_task._check_live_store_access(_spec(
            acceptance=["Nothing in this bead reads the live store."],
        ))

    def test_none_negates_like_no(self):
        file_task._check_live_store_access(_spec(
            acceptance=["None of this touches the prod store."],
        ))

    def test_without_is_a_negation_cue(self):
        file_task._check_live_store_access(_spec(
            acceptance=[
                "THE fixture is supplied here, without any read from the "
                "live store."
            ],
        ))

    def test_neither_is_a_negation_cue(self):
        file_task._check_live_store_access(_spec(
            acceptance=[
                "Neither contains 'live store' nor 'prod store' as a "
                "direction."
            ],
        ))

    # --- F3 (release gate on PR #888): `at execution` requires a
    # co-occurring measurement word. Re-derived by the outer loop: bare `at
    # execution` fired on prose that never measures anything, using
    # vocabulary pervasive in this repo's dispatcher specs.

    def test_at_execution_without_a_measurement_word_does_not_refuse_lease(self):
        file_task._check_live_store_access(_spec(
            acceptance=[
                "THE lease SHALL be acquired at execution time by the "
                "Temporal activity."
            ],
        ))

    def test_at_execution_without_a_measurement_word_does_not_refuse_digest(self):
        file_task._check_live_store_access(_spec(
            acceptance=[
                "The registry digest SHALL be pinned at execution, not at "
                "build."
            ],
        ))

    def test_at_execution_with_a_measurement_word_still_refuses(self):
        with pytest.raises(SystemExit):
            file_task._check_live_store_access(_spec(
                acceptance=[
                    "THE count SHALL be re-measured at execution, not read "
                    "from a snapshot."
                ],
            ))

    # --- F4 (release gate on PR #888): `substrate[_ ]?url` requires a
    # co-occurring read/imperative verb. M7a's real acceptance[3] refused on
    # a criterion about error handling when the variable is UNSET -- the
    # inverse of a direction at the store.

    def test_substrate_url_unset_error_handling_does_not_refuse(self):
        file_task._check_live_store_access(_spec(
            acceptance=[
                "With SUBSTRATE_URL or SUBSTRATE_API_KEY unset, the script "
                "SHALL exit with the same code as before."
            ],
        ))

    def test_substrate_url_with_a_read_verb_still_refuses(self):
        with pytest.raises(SystemExit):
            file_task._check_live_store_access(_spec(
                verification={"commands": ["curl $SUBSTRATE_URL/beads | jq ."]},
            ))

    # --- AC-8 (release gate on PR #888, finding F2): a negation SHALL
    # govern the reference it cancels, not merely sit within the word
    # window. These three filed CLEAN at #888's head -- an unrelated
    # negation earlier in a compound sentence laundered a real direction at
    # the live store past the 8-word window. Pinned here as known-bad
    # fixtures that SHALL now refuse.

    def test_unrelated_negation_does_not_launder_a_live_store_direction(self):
        """'not' governs 'a drill', not 'read the live store' -- ', so '
        opens a fresh, unnegated clause."""
        with pytest.raises(SystemExit):
            file_task._check_live_store_access(_spec(
                acceptance=[
                    "This is not a drill, so read the live store and "
                    "report the count."
                ],
            ))

    def test_unrelated_negation_does_not_launder_a_shall_directive(self):
        """'No' governs 'shortcuts', not 'SHALL read' -- the colon opens a
        fresh mandate with its own SHALL."""
        with pytest.raises(SystemExit):
            file_task._check_live_store_access(_spec(
                acceptance=[
                    "No shortcuts: the worker SHALL read the live "
                    "production store directly."
                ],
            ))

    def test_unrelated_negation_does_not_launder_the_exact_d7_shape(self):
        """'no' governs 'a fixture', not 'measure against the prod store' --
        this is the exact phrasing D7 exists for: no committed fixture, so
        measure live."""
        with pytest.raises(SystemExit):
            file_task._check_live_store_access(_spec(
                acceptance=[
                    "There is no fixture, so measure against the prod "
                    "store yourself."
                ],
            ))

    def test_colon_elaborating_a_negation_without_its_own_shall_stays_clean(self):
        """S53-2's real committed intent text (apps/factory-dispatcher/
        tasks/S53-2-arch-risk-leaves-the-gaps-list.json), reproduced
        verbatim: the colon here elaborates what 'NOT THIS BEAD'S' refers
        to, and the elaboration states no SHALL of its own -- unlike the
        two fixtures above, this colon must NOT open a negation-blocking
        clause, or a spec that was never in violation starts refusing."""
        file_task._check_live_store_access(_spec(
            intent=(
                "FILING THE FIRST THREE RISKS IS NOT THIS BEAD'S: writing "
                "beads to the prod store is an operator act with "
                "credentials the verification sandbox does not hold; the "
                "outer loop files them once this lands (S53-3)."
            ),
        ))

    # -----------------------------------------------------------------------
    # ADDED 2026-09-17 at the #911 gate (F1). These three are the INVERSE of
    # ad0a0f95's AC-5 -- the sentences a filer writes when they are COMPLYING
    # with D7 and saying so. They refused, because
    # _execution_time_measurement_hit anchored on _MEASUREMENT_WORD_RE, whose
    # `the\s+count` alternative matches at index 0, so _is_negated inspected
    # zero preceding words and never saw a NOT sitting after the anchor.
    # Refusing the correctly-written spec is worse than cosmetic: the message
    # tells the filer to set live_store_access, producing a false declaration
    # on a bead that needs none -- or a reword, and "if the escape hatch is
    # awkward, the filer rewords around the check and the control is worth
    # nothing" is this bead's own reasoning.
    # These fail at the pre-fix head of this PR and are clean at #888's head,
    # so they pin a regression this PR introduced, not a pre-existing gap.
    # -----------------------------------------------------------------------

    def test_a_prohibition_on_re_measuring_at_execution_stays_clean(self):
        file_task._check_live_store_access(_spec(
            acceptance=[
                "THE COUNT SHALL NOT be re-measured at execution; it is "
                "inherited from the committed snapshot."
            ],
        ))

    def test_a_prohibition_on_measuring_at_execution_time_stays_clean(self):
        file_task._check_live_store_access(_spec(
            acceptance=["The count SHALL NOT be measured at execution time."],
        ))

    def test_a_prohibition_after_a_semicolon_clause_stays_clean(self):
        file_task._check_live_store_access(_spec(
            acceptance=[
                "The committed snapshot SHALL be used; the count SHALL NOT "
                "be re-measured at execution."
            ],
        ))

    def test_the_positive_execution_time_measurement_still_refuses(self):
        """The control for the three above: the ad0a0f95 AC-5 shape they are
        the inverse of must still refuse, or the fix has simply disabled the
        detector rather than corrected its anchor."""
        with pytest.raises(SystemExit):
            file_task._check_live_store_access(_spec(
                acceptance=[
                    "THE COUNT SHALL BE RE-MEASURED AT EXECUTION, NOT "
                    "INHERITED FROM THIS SPEC."
                ],
            ))


# ---------------------------------------------------------------------------
# corpus regression — "every spec currently in apps/factory-dispatcher/tasks/
# SHALL still pass the new checks or be reported" (this bead's own AC). Run
# against build_content only: it needs no live Substrate, so this is the part
# of filing every spec in the corpus can actually be checked against inside a
# test run. The predecessor/supersession and worker_hint/duplicate checks that
# do need live board state are exercised by fixtures above instead.
# ---------------------------------------------------------------------------

_TASKS_DIR = pathlib.Path(__file__).resolve().parents[1] / "tasks"
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


def _tracked_task_spec_paths() -> list[pathlib.Path]:
    """The task specs `git` actually tracks directly under
    apps/factory-dispatcher/tasks/ (not tasks/done/, tasks/snapshots/, or
    any other subdirectory) -- driven from ``git ls-files`` rather than a
    filesystem glob.

    RE-DERIVED 2026-09-17 (release gate DO-NOT-MERGE on PR #888, finding F6,
    carried from the #883 gate and not addressed in #888): ``_TASKS_DIR.glob
    ("*.json")`` sees whatever is on an operator's disk, untracked scratch
    specs included. Measured 2026-09-16: 79 tracked specs and 7 refusals in
    CI, 113 specs and 9 refusals in an operator's checkout -- green in CI,
    red locally, with nothing saying which corpus either number described.
    ``git ls-files`` is a local, no-network, no-store read (consistent with
    this bead's own "no network, no store, in any test" requirement), so
    both views now see the same corpus regardless of what an operator has
    lying around uncommitted.

    Filtered to files whose PARENT is exactly this directory: a naive
    ``grep -v '/done/'`` over ``git ls-files`` output still passes through
    ``tasks/snapshots/*.json`` (a real tracked file in this repo,
    2026-09-15-application-dependency-posture.json) -- checking the parent
    path directly is what the non-recursive glob this replaces was already
    doing, and is what keeps this test's scope unchanged.
    """
    result = subprocess.run(
        ["git", "ls-files", "--", str(_TASKS_DIR.relative_to(_REPO_ROOT))],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    tasks_rel = _TASKS_DIR.relative_to(_REPO_ROOT)
    paths = []
    for line in result.stdout.splitlines():
        rel = pathlib.Path(line)
        if rel.parent != tasks_rel or rel.suffix != ".json":
            continue
        paths.append(_REPO_ROOT / rel)
    return sorted(paths)

#: Refused by _check_boundary_principal (root-cause track control C-4): the
#: spec's scope.paths intersects BOUNDARY_PATHS and it carries no principal/
#: reaches. Generated once, from this PR's base (e613ba9b plus this PR's own
#: diff), by running build_content() over every tracked spec and recording
#: every C-4 refusal -- 90 of 129. These are historical specs filed before C-4
#: existed; the file-and-live-bead rule means editing them is drift, so they
#: are reported here for the outer loop to disposition, exactly like the two
#: sets below. Checked FIRST in the test below: _check_boundary_principal runs
#: before _check_traceability in build_content, so a spec in both this set and
#: _KNOWN_MISSING_REQUIREMENT_REFS (OPS-102/103/104) is actually refused for
#: THIS reason now, not the older one.
_KNOWN_MISSING_BOUNDARY_PRINCIPAL = frozenset(
    {
        "A34-1-a-merge-verdict-executes-without-a-person-behind-a-buffer.json",
        "FA-S49-1-declared-verification-passes-what-ci-fails.json",
        "FA-S49-2-a-spec-can-strand-its-own-bead-at-filing.json",
        "M1-commit-the-substrates-contract.json",
        "M11-finance-routes-and-crypto-behind-the-registered-namespace.json",
        "M12-mcp-hub-on-the-one-client.json",
        "M2-scope-registry-one-declared-set.json",
        "M3-severity-vocabulary-parity.json",
        "M4-execute-amendment-31-code-half.json",
        "M5-move-the-intake-contract-to-its-owner.json",
        "M6-one-python-substrate-client.json",
        "M8-dispatcher-activities-on-the-one-client.json",
        "M9-factory-intake-as-a-gateway-capability.json",
        "OPS-100-a-self-review-artifact-discards-the-whole-run.json",
        "OPS-101-the-hub-image-cannot-import-the-app-it-ships.json",
        "OPS-102-the-checkout-advance-guard-lost-its-source-of-exclusivity.json",
        "OPS-103-a-red-build-is-announced-to-nobody.json",
        "OPS-104-a-lost-short-step-wedges-the-queue-for-a-day.json",
        "OPS-106-preserved-work-is-recorded-and-never-handed-to-the-retry.json",
        "OPS-107-a-robot-owner-cannot-downgrade-its-own-governed-fact.json",
        "OPS-109-nothing-notices-that-a-deploy-has-not-happened.json",
        "OPS-110-the-factory-endpoints-answer-unknown-forever-in-prod.json",
        "OPS-114-observation-writers-enrolled-for-source-class-observed.json",
        "OPS-115-ancestry-check-fetches-the-local-origin.json",
        "OPS-116-scanner-calls-a-same-identity-refile-superseded.json",
        "OPS-117-resumed-worker-brief-leads-with-the-review-note.json",
        "OPS-118-observation-writers-declare-source-class-observed.json",
        "OPS-119-enrol-every-factory-reconciler-for-the-class-it-writes.json",
        "OPS-120-every-factory-reconciler-declares-the-class-it-writes.json",
        "OPS-121-a-read-only-substrate-key-for-clients-that-only-read.json",
        "OPS-122-the-worker-agent-holds-only-the-read-key.json",
        "OPS-14-cluster-health-runs-on-a-schedule.json",
        "OPS-155-a-pre-existing-failure-claim-is-recorded-unmeasured.json",
        "OPS-158-the-source-class-keep-or-revert-decision-has-no-carrier.json",
        "OPS-182-the-finance-integrity-mapper-relabels-every-unique-violation.json",
        "OPS-184-a-session-limit-is-not-a-work-failure.json",
        "OPS-185-the-base-revision-memo-is-worker-writable.json",
        "OPS-188-schedule-status-renders-not-checked-for-the-base-ref.json",
        "OPS-66-a-capacity-pause-waits-for-a-human-who-was-told-to-wait.json",
        "OPS-67-the-tunnel-the-factory-lives-on-dies-with-the-session-that-started-it.json",
        "OPS-68-two-filers-pass-the-duplicate-guard-together.json",
        "OPS-70-a-lane-executed-bead-is-invisible-to-reconciliation.json",
        "OPS-73-a-registered-activity-with-no-startable-workflow-is-invisible.json",
        "OPS-74-the-standing-status-bead-helpers-are-copied-not-extracted.json",
        "OPS-75-check-view-is-wired-to-no-schedule-and-the-registry-drifts.json",
        "OPS-77-two-tests-fail-in-any-exported-tree.json",
        "OPS-78-source-class-ownership-fires-on-patch-but-not-on-post.json",
        "OPS-79-the-wedge-detector-asserts-a-measurement-it-never-made.json",
        "OPS-80-the-worker-verifies-everything-except-the-lint-that-gates-the-merge.json",
        "OPS-81-the-readme-names-schedules-nothing-ties-to-the-code.json",
        "OPS-82-a-retry-rebuilds-because-nothing-hands-it-what-survived.json",
        "OPS-83-two-timestamp-formats-land-in-sibling-observed-at-fields.json",
        "OPS-84-the-completeness-invariant-does-not-look-backward.json",
        "OPS-85-the-first-landing-state-mutation-survives.json",
        "OPS-86-no-rule-says-who-may-claim-a-source-class.json",
        "OPS-87-the-update-paths-state-stamp-is-unpinned.json",
        "OPS-88-the-spec-directory-claims-to-be-the-queue.json",
        "OPS-95-two-requests-strip-a-governed-facts-owner-lock.json",
        "OPS-96-links-land-attributed-unknown.json",
        "OPS-97-sixty-change-records-carry-the-misleading-string.json",
        "OPS-98-a-sibling-store-method-is-tested-only-through-the-fake.json",
        "OPS-99-one-activity-slot-is-shared-by-the-dispatch-and-every-reconciler.json",
        "R2605-1-an-incident-is-a-type-that-exists-with-a-machine-above-the-store.json",
        "R2605-2-a-merged-pull-request-writes-the-change-record-it-is.json",
        "R2605-3-only-a-declared-alert-may-open-an-incident.json",
        "R2605-5b-namespace-schemas-registered-on-the-path-that-runs.json",
        "R2605-6-the-csdm-layer-is-not-an-artifact-anyone-could-install.json",
        "R2605-7-the-findings-carrier-has-no-machine-and-the-model-docs-have-not-converged.json",
        "R2605-8-the-technology-layer-is-empty-and-every-surface-says-it-is-fine.json",
        "R2605-9-the-incident-record-moves-nothing.json",
        "S51-3-the-observer-lands-arch-ci-from-the-cluster-it-can-reach.json",
        "S52-1-the-gateway-has-no-note-capability-so-half-an-intake-is-still-open.json",
        "S52-2-merge-by-verdict-is-a-dark-capability-that-executes-only-a-gate-written-merge.json",
        "S52-3-depends-on-resolved-from-the-cluster-not-guessed.json",
        "S52-4-the-phone-path-is-proven-by-a-dated-probe-not-a-demo.json",
        "S52-5-every-gateway-call-is-attributable-by-a-check.json",
        "S53-2-arch-risk-leaves-the-gaps-list.json",
        "S53-4-ea-coverage-reports-the-technology-layer-as-a-trend.json",
        "S54-1-the-bd-dolt-set-moves-to-the-archive.json",
        "S54-2-the-codex-worker-entry-is-deleted.json",
        "S54-4-scripts-nothing-invokes-are-deleted.json",
        "S55-B2-selector-and-breaker-move-verbatim-into-queue-order.json",
        "S55-B3-breaker-exits-on-worker-finished-or-requeue.json",
        "SEC-019909c0-75b50b58-dd648709-the-worker-and-its-verification-cannot-read-host-credentials-connect-to-port-22-or-reach-loopback-services.json",
        "SEC-52deaf69-the-hub-verifies-the-access-assertion-instead-of-trusting-identity-headers.json",
        "SEC-6c19f60f-declared-verification-runs-inside-the-worker-sandbox.json",
        "SEC-a0166920-1-the-factory-processes-start-with-no-credential-in-their-exec-environment.json",
        "SEC-a0166920-2-every-child-the-dispatcher-spawns-gets-an-explicit-credential-free-environment.json",
        "SEC-bd4b2a9a-32f631c1-the-dispatcher-runs-no-clone-code-when-it-materializes-personas-or-smoke-checks.json",
        "SEC-dd648709-a-temporal-payload-cannot-choose-the-dispatchers-config.json",
    }
)

#: Refused today by the pre-existing _check_traceability (no requirement_refs
#: and no waiver) -- a policy older than and unrelated to this bead's new
#: checks. Named explicitly so a refusal this bead's OWN checks newly
#: introduce shows up as an unexpected addition to this set, not silently
#: blended into an already-known list.
_KNOWN_MISSING_REQUIREMENT_REFS = frozenset(
    {
        "OPS-101-the-hub-image-cannot-import-the-app-it-ships.json",
        "OPS-102-the-checkout-advance-guard-lost-its-source-of-exclusivity.json",
        "OPS-103-a-red-build-is-announced-to-nobody.json",
        "OPS-104-a-lost-short-step-wedges-the-queue-for-a-day.json",
        "OPS-105-verifying-a-secret-store-session-printed-two-secrets.json",
    }
)

#: Refused by this bead's OWN new check (_check_live_store_access, D7):
#: acceptance/verification/intent text in these specs points at the live or
#: production store, with no negation cue the heuristic recognises and no
#: live_store_access declaration. Measured 2026-09-16 by running
#: build_content() over the whole corpus AFTER the release-gate recalibration
#: (F2/F3 on PR #883, head 9c43521c): bare `prod` now requires an adjacent
#: store noun, and nothing/none/without/neither/nor are recognised negation
#: cues. That recalibration shrank this set from 15 to 7 -- M4, OPS-107,
#: OPS-109, OPS-110, OPS-114, OPS-121, OPS-122 and S51-3 all fired only on
#: bare `prod` (a k8s namespace, a symptom, a past measurement) or missed a
#: "nothing"/"none" negation, and now file clean. This bead's own ACs say the
#: worker must NOT edit the remaining specs to make them pass -- several are
#: live beads, and the file-and-live-bead rule means editing one half is
#: drift -- so they are reported here, exactly like the pre-existing
#: requirement_refs exceptions above, for the outer loop to disposition.
#: OPS-116 in particular is the same spec CLAUDE.md's D7 clause names as the
#: original violation behind dev.finding c48a3827 ("OPS-116's own AC-4 asked
#: it to measure 'against one store read'") -- this heuristic catching it is
#: a good sign, not a false positive to chase away. OPS-119 and S52-3 each
#: carry a declaration-shaped sentence too ("the worker does NOT read the
#: store", "not from ea-coverage.py") that sits in a different clause or on
#: the wrong side of the match from the live-store mention it governs -- a
#: real remaining gap in the negation heuristic, left as a reported exception
#: rather than chased with more clause-boundary tuning.
_KNOWN_LIVE_STORE_ACCESS_HITS = frozenset(
    {
        "OPS-116-scanner-calls-a-same-identity-refile-superseded.json",
        "OPS-119-enrol-every-factory-reconciler-for-the-class-it-writes.json",
        "OPS-97-sixty-change-records-carry-the-misleading-string.json",
        "R2605-7-the-findings-carrier-has-no-machine-and-the-model-docs-have-not-converged.json",
        "R2605-8-the-technology-layer-is-empty-and-every-surface-says-it-is-fine.json",
        "R2605-9-the-incident-record-moves-nothing.json",
        "S52-3-depends-on-resolved-from-the-cluster-not-guessed.json",
    }
)


def test_every_filed_spec_in_the_corpus_still_builds_or_is_a_known_exception():
    unexpected = []
    for path in _tracked_task_spec_paths():
        spec = json.loads(path.read_text())
        try:
            file_task.build_content(spec)
        except SystemExit as exc:
            message = str(exc)
            # Checked FIRST: _check_boundary_principal runs before
            # _check_traceability in build_content, so a spec missing BOTH
            # principal/reaches and requirement_refs is actually refused for
            # this reason, not the older one below (OPS-102/103/104).
            if path.name in _KNOWN_MISSING_BOUNDARY_PRINCIPAL:
                assert "control C-4" in message, (
                    f"{path.name} is refused for a NEW reason, not the known "
                    f"missing-principal/reaches one: {exc}"
                )
            elif path.name in _KNOWN_MISSING_REQUIREMENT_REFS:
                assert "requirement_refs" in message, (
                    f"{path.name} is refused for a NEW reason, not the known "
                    f"missing-requirement_refs one: {exc}"
                )
            elif path.name in _KNOWN_LIVE_STORE_ACCESS_HITS:
                assert "live/production store" in message, (
                    f"{path.name} is refused for a NEW reason, not the known "
                    f"live-store-access one: {exc}"
                )
            else:
                unexpected.append(f"{path.name}: {exc}")
    assert unexpected == []


def test_the_known_exceptions_still_exist_in_the_corpus():
    """If one of these specs is removed or re-filed, this set should shrink
    with it -- a stale entry here would silently widen what the test above
    tolerates."""
    present = {path.name for path in _tracked_task_spec_paths()}
    assert _KNOWN_MISSING_BOUNDARY_PRINCIPAL <= present
    assert _KNOWN_MISSING_REQUIREMENT_REFS <= present
    assert _KNOWN_LIVE_STORE_ACCESS_HITS <= present


# ---------------------------------------------------------------------------
# derived_from_finding_ids — S55-10a: a dev.task filed from a dev.finding
# carries an edge back to it, dev.task --derived_from--> dev.finding (the
# direction of the 103 existing hand-written edges of this shape).
# ---------------------------------------------------------------------------


def _finding(finding_id, state="backlogged"):
    return {
        "id": finding_id,
        "namespace": "dev",
        "type": "finding",
        "state": state,
        "content": {},
    }


class TestDerivedFromFindingIds:
    def test_build_content_passes_the_list_through_unchanged(self):
        content = file_task.build_content(_spec(derived_from_finding_ids=["f-1"]))
        assert content["derived_from_finding_ids"] == ["f-1"]

    def test_absent_by_default(self):
        content = file_task.build_content(_spec())
        assert "derived_from_finding_ids" not in content

    def test_empty_list_builds_with_no_key(self):
        """c2c52aaf AC-3: an empty list keeps today's behaviour -- treated as
        absent, no key in content -- rather than being refused."""
        content = file_task.build_content(_spec(derived_from_finding_ids=[]))
        assert "derived_from_finding_ids" not in content

    def test_a_non_list_is_refused_naming_the_field(self):
        with pytest.raises(SystemExit) as exc:
            file_task.build_content(_spec(derived_from_finding_ids="f-1"))
        assert "derived_from_finding_ids must be a list of dev.finding ids" in str(exc.value)
        assert "str" in str(exc.value)

    def test_a_non_string_item_is_refused_naming_its_index(self):
        with pytest.raises(SystemExit) as exc:
            file_task.build_content(_spec(derived_from_finding_ids=["f-1", 5]))
        message = str(exc.value)
        assert "derived_from_finding_ids[1]" in message
        assert "int" in message

    def test_an_empty_string_item_is_refused_naming_its_index(self):
        with pytest.raises(SystemExit) as exc:
            file_task.build_content(_spec(derived_from_finding_ids=["f-1", ""]))
        message = str(exc.value)
        assert "derived_from_finding_ids[1]" in message
        assert "got the empty string" in message

    def test_a_string_of_one_id_is_refused_before_any_list_beads_call(
        self, tmp_path, monkeypatch
    ):
        """c2c52aaf: today this is refused naming the first character, after
        one list_beads call -- the string is iterated char by char by
        _check_derived_from_findings. The fix refuses it in build_content,
        naming the field, with no list_beads call at all."""
        fake = FakeSubstrate([], beads=[_finding("f-1")])
        calls = []
        original_list_beads = fake.list_beads

        def _tracked(*args, **kwargs):
            calls.append((args, kwargs))
            return original_list_beads(*args, **kwargs)

        monkeypatch.setattr(fake, "list_beads", _tracked)
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = self._file(tmp_path, derived_from_finding_ids="f-1")

        with pytest.raises(SystemExit) as exc:
            file_task.main([str(spec_path)])

        message = str(exc.value)
        assert "derived_from_finding_ids must be a list of dev.finding ids" in message
        assert calls == []
        assert fake.posted is None

    def _file(self, tmp_path, **overrides):
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(_spec(**overrides)))
        return spec_path

    def test_refuses_an_id_absent_from_the_newest_page(self, tmp_path, monkeypatch):
        fake = FakeSubstrate([], beads=[_finding("f-1")])
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = self._file(tmp_path, derived_from_finding_ids=["f-missing"])

        with pytest.raises(SystemExit) as exc:
            file_task.main([str(spec_path)])

        message = str(exc.value)
        assert "f-missing" in message
        assert "not among the newest 500 dev.findings" in message
        assert fake.posted is None
        assert fake.links == []

    def test_refuses_an_id_naming_a_dev_task_not_a_finding(self, tmp_path, monkeypatch):
        """AC-10: a derived_from id that resolves to a dev.task must refuse --
        list_beads("dev", "finding", ...) never returns a dev.task, so this
        falls out of the same membership check with no special-casing."""
        fake = FakeSubstrate([{"id": "task-1", "state": "pending", "content": {}}])
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = self._file(tmp_path, derived_from_finding_ids=["task-1"])

        with pytest.raises(SystemExit) as exc:
            file_task.main([str(spec_path)])

        assert "task-1" in str(exc.value)
        assert fake.posted is None

    def test_writes_one_derived_from_edge_per_id_with_the_task_as_source(
        self, tmp_path, monkeypatch
    ):
        fake = FakeSubstrate([], beads=[_finding("f-1"), _finding("f-2")])
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = self._file(tmp_path, derived_from_finding_ids=["f-1", "f-2"])

        rc = file_task.main([str(spec_path)])

        assert rc == 0
        assert fake.posted is not None  # the task bead itself was filed
        # create_task's fake always answers "new-bead-id" — assert the edges
        # were written with that id as source and each finding as target.
        assert fake.links == [
            ("new-bead-id", "f-1", "derived_from", file_task.CREATED_BY),
            ("new-bead-id", "f-2", "derived_from", file_task.CREATED_BY),
        ]

    def test_an_add_link_failure_is_reported_by_hand_with_no_delete(
        self, tmp_path, monkeypatch
    ):
        fake = FakeSubstrate([], beads=[_finding("f-1")])

        def _boom(*args, **kwargs):
            raise RuntimeError("409 conflict")

        monkeypatch.setattr(fake, "add_link", _boom)
        monkeypatch.setattr(file_task, "Substrate", lambda: fake)
        spec_path = self._file(tmp_path, derived_from_finding_ids=["f-1"])

        with pytest.raises(SystemExit) as exc:
            file_task.main([str(spec_path)])

        message = str(exc.value)
        assert "new-bead-id" in message
        assert "f-1" in message
        assert fake.posted is not None  # the task bead itself was still filed
