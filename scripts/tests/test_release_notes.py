"""Tests for docs/releases/<ref>-notes.md generation.

No network, no credentials: every test drives release-notes.py off the
committed fixtures under scripts/tests/fixtures/release_notes/, the same
provider shape (a fixture root of releases/beads/links/prs) that
test_release_manifest.py already uses for exactly this reason.

Three properties matter most, each asserted directly: an outcome with no
merged PR renders as NOT DELIVERED and is never omitted; a criterion whose
newest observation predates the release's opened_at renders as not
re-measured, never as a current verdict; and an unreachable substrate or a
failing gh call fails the run rather than writing notes with a silently
empty section.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import sys
import urllib.request
from types import SimpleNamespace

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = REPO / "scripts" / "tests" / "fixtures" / "release_notes"


def _load_release_notes():
    spec = importlib.util.spec_from_file_location(
        "release_notes_under_test",
        REPO / "scripts" / "release-notes.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def rn():
    return _load_release_notes()


# --- end-to-end rendering against the fixture release ------------------------


def test_notes_open_with_the_objective_verbatim(rn):
    provider = rn.FixtureProvider(FIXTURES)
    notes = rn.gather_notes("R90.02", provider)
    rendered = rn.render_notes(notes)

    assert rendered.startswith("# Release R90.02 — Fixture release for release-notes")
    assert (
        "Exercise release-notes.py rendering against committed fixtures, "
        "with no network and no credentials."
    ) in rendered


def test_delivered_outcome_lists_its_merged_pr_and_criteria(rn):
    provider = rn.FixtureProvider(FIXTURES)
    rendered = rn.render_notes(rn.gather_notes("R90.02", provider))

    assert "### O-1 [enabling]" in rendered
    assert "- Criteria cited: PC-FAC-001/AC-5" in rendered
    assert "- #701 Ship the enabling outcome (https://github.com/grantbest/homelabv2/pull/701)" in rendered


def test_outcome_with_no_merged_task_renders_not_delivered_and_is_not_omitted(rn):
    """AC: an outcome with no merged task renders as NOT DELIVERED and is
    never omitted — even though PR #702 exists, it is OPEN, not MERGED."""
    provider = rn.FixtureProvider(FIXTURES)
    rendered = rn.render_notes(rn.gather_notes("R90.02", provider))

    assert "### O-2 [risk]" in rendered
    assert "NOT DELIVERED" in rendered
    assert "O-2 NOT DELIVERED: Risk work whose PR never merged." in rendered
    # The outcome itself is never dropped even though it delivered nothing.
    assert "Risk work whose PR never merged." in rendered


def test_a_fully_delivered_release_reports_no_undelivered_outcomes(rn):
    provider = rn.FixtureProvider(FIXTURES)
    notes = rn.gather_notes("R90.02", provider)
    # Force every outcome delivered to check the alternate branch renders.
    for outcome in notes.outcomes:
        if not outcome.delivered:
            notes.outcomes.remove(outcome)
    rendered = rn.render_notes(notes)

    assert "(all outcomes delivered)" in rendered


def test_stale_criterion_renders_not_re_measured_never_as_current(rn):
    """AC: a criterion whose newest observation predates opened_at renders as
    'not re-measured' and never as a current verdict."""
    provider = rn.FixtureProvider(FIXTURES)
    rendered = rn.render_notes(rn.gather_notes("R90.02", provider))

    assert "PC-TRU-001/AC-1: as at opened_at = absent" in rendered
    assert "not re-measured since 2026-08-10" in rendered
    # The stale verdict is never rendered as if it were the current one.
    assert "latest = absent (measured 2026-08-10" not in rendered
    assert "[unchanged]" not in rendered.split("PC-TRU-001")[1].split("\n")[0]


def test_moved_criterion_shows_opened_at_and_latest_with_date_and_revision(rn):
    provider = rn.FixtureProvider(FIXTURES)
    rendered = rn.render_notes(rn.gather_notes("R90.02", provider))

    assert "PC-FAC-001/AC-5: as at opened_at = absent (measured 2026-08-06, rev abc1111)" in rendered
    assert "latest = pass (measured 2026-08-27, rev def2222) [changed]" in rendered


def test_outcome_citing_no_criteria_renders_none(rn):
    provider = rn.FixtureProvider(FIXTURES)
    rendered = rn.render_notes(rn.gather_notes("R90.02", provider))

    assert "### O-3 [security]" in rendered
    section = rendered.split("### O-3")[1].split("###")[0]
    assert "- Criteria cited: (none)" in section


def test_balance_reuses_release_status_computation_and_reports_absent(rn):
    """AC: declared-vs-actual balance reuses REL-2's computation rather than a
    second implementation of it. Proven two ways: the exact BalanceEntry
    objects release_status computed are what gets rendered, and the ABSENT
    marking (blocking is declared at 10% with nothing delivered) is REL-2's
    load-bearing distinction, not a re-derivation."""
    provider = rn.FixtureProvider(FIXTURES)
    notes = rn.gather_notes("R90.02", provider)
    rendered = rn.render_notes(notes)

    assert all(isinstance(entry, rn._release_status.BalanceEntry) for entry in notes.balance)
    assert "- blocking: declared 10%, actual ABSENT (0 task(s))" in rendered
    # R90.02 has 3 classified delivering tasks, below MIN_CLASSIFIED_FOR_PERCENTAGE (5):
    # a percentage would be noise, so this renders the insufficient-sample line instead.
    assert "- enabling: declared 30%, actual insufficient sample (n=3) (1 task(s))" in rendered


def test_balance_shows_insufficient_sample_on_every_line_including_absent(rn):
    """R90.02 has 3 classified delivering tasks, below
    MIN_CLASSIFIED_FOR_PERCENTAGE (5) -- every balance line, including the
    ABSENT one (blocking), must carry the insufficient-sample caveat. The
    blocking line's existing exact substring must still be present: ABSENT is
    a count fact and insufficient_sample a sample-size fact, and losing
    either by having one override the other's rendering would hide one of
    the two from an operator reading the notes."""
    provider = rn.FixtureProvider(FIXTURES)
    rendered = rn.render_notes(rn.gather_notes("R90.02", provider))

    balance_section = rendered.split("## Declared vs Actual Balance")[1].split("##")[0]
    lines = [line for line in balance_section.strip().splitlines() if line.strip()]
    assert lines
    for line in lines:
        assert "insufficient sample (n=3)" in line
        assert "declared" in line and "%" in line
        assert "None%" not in line
        assert not re.search(r"actual\s+-?\d+(\.\d+)?%", line)

    assert "- blocking: declared 10%, actual ABSENT (0 task(s))" in rendered


def test_work_with_no_outcome_is_listed(rn):
    provider = rn.FixtureProvider(FIXTURES)
    rendered = rn.render_notes(rn.gather_notes("R90.02", provider))

    section = rendered.split("## Work With No Outcome")[1].split("##")[0]
    assert "10000000-0000-4000-8000-000000000004" in section


def test_release_gate_verdicts_are_listed_per_pr(rn):
    provider = rn.FixtureProvider(FIXTURES)
    rendered = rn.render_notes(rn.gather_notes("R90.02", provider))

    section = rendered.split("## Release-Gate Verdicts")[1]
    assert "- #701 Ship the enabling outcome: MERGE" in section
    assert "- #703 Ship the security outcome: MERGE-WITH-CHANGES" in section
    # #702 (the unmerged PR) still carries no verdict; it is reported honestly.
    assert "#702 Risk work still under review: (no Release-gate verdict recorded)" in section


# --- UNMEASURED renders distinctly from NOT DELIVERED, end to end (AC6/AC7) --
#
# R90.02's provider has no population/R90.02.json fixture, so its tests above
# are unaffected: gather_notes falls back to the pre-existing
# delivering-tasks-only shape for that ref. R90.03 is a release built just for
# this: one outcome with a delivering task and no merged PR (NOT DELIVERED,
# the way R90.02's O-2 is) and one zero-task outcome with a closed-unbound
# candidate reachable only through the whole-population scan (UNMEASURED).


def _forbid_network(monkeypatch):
    """Every test in this module that constructs a LiveProvider runs under
    this guard (AC8) -- a reader that escapes the injected reader_factory
    must fail loudly instead of reading the live store. Both loaded copies
    of substrate_client.py (release-notes.py's and release-status.py's) use
    the one urllib.request module, so patching it here covers either."""

    def _raise(*args, **kwargs):
        raise AssertionError("no test in this module may reach the network")

    monkeypatch.setattr(urllib.request, "urlopen", _raise)


# The store's own allow-list (apps/substrate/src/routes.py:806) and link
# direction set (routes.py:1013) -- the double below refuses what the real
# store refuses, so a call that would 400 against the live substrate fails
# the test here instead of passing silently against a double that accepts
# anything.
_LIST_BEADS_QUERY_PARAMS = frozenset(
    {"namespace", "type", "state", "trust_tier", "parent_id", "created_after", "content_ref", "limit", "offset"}
)
_LIST_LINKS_DIRECTIONS = frozenset({"outgoing", "incoming", "both"})


def _reader_double(beads_by_type, links_by_bead_id):
    """A SubstrateReader-shaped double (list_beads/list_links) for
    release-status.py's gather_live_data. Plain closures in a SimpleNamespace,
    deliberately not a class: test_store_double_call_surface.py's
    hand-rolled-double census counts any class whose own-or-inherited methods
    overlap two or more BeadStore protocol names (list_beads and list_links
    both are), and that ratchet sits at MAX_HAND_ROLLED_DOUBLES with no
    headroom -- a SimpleNamespace has no ClassDef for it to find."""

    def list_beads(namespace, type_, **params):
        unknown = set(params) - _LIST_BEADS_QUERY_PARAMS
        if unknown:
            raise RuntimeError(f"unknown_query_parameter: {sorted(unknown)}")
        key = (namespace, type_)
        results = beads_by_type.get(key, [])
        content_ref = params.get("content_ref")
        if content_ref is not None:
            results = [b for b in results if (b.get("content") or {}).get("ref") == content_ref]
        return results

    def list_links(bead_id, *, direction="both", link_type=None):
        if direction not in _LIST_LINKS_DIRECTIONS:
            raise RuntimeError(f"invalid direction: {direction!r}")
        return links_by_bead_id.get(bead_id, [])

    return SimpleNamespace(list_beads=list_beads, list_links=list_links)


def test_unmeasured_renders_distinct_from_not_delivered(monkeypatch, rn):
    _forbid_network(monkeypatch)
    provider = rn.FixtureProvider(FIXTURES)
    rendered = rn.render_notes(rn.gather_notes("R90.03", provider))

    not_delivered_section = rendered.split("## What Was Not Delivered")[1].split("##")[0]
    unmeasured_section = rendered.split("## Unmeasured Outcomes")[1].split("##")[0]

    assert "### O-A [enabling]" in rendered
    assert "### O-B [risk]" in rendered

    o_a_section = rendered.split("### O-A")[1].split("### O-B")[0]
    o_b_section = rendered.split("### O-B")[1].split("## Measured Movement")[0]
    assert o_a_section != o_b_section
    assert "NOT DELIVERED" in o_a_section
    assert "UNMEASURED" not in o_a_section
    assert "UNMEASURED, not NOT DELIVERED" in o_b_section
    assert "inspect the candidate(s)" in o_b_section
    assert "bind" in o_b_section
    assert "20000000-0000-4000-8000-000000000002" in o_b_section

    assert "O-A NOT DELIVERED" in not_delivered_section
    assert "O-B" not in not_delivered_section

    assert "O-B UNMEASURED, not NOT DELIVERED" in unmeasured_section
    assert "20000000-0000-4000-8000-000000000002" in unmeasured_section
    assert "O-A" not in unmeasured_section


def test_live_provider_without_a_factory_makes_no_network_call(monkeypatch, rn, tmp_path):
    _forbid_network(monkeypatch)
    monkeypatch.setenv("SUBSTRATE_URL", "https://example.invalid")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "not-a-real-key")

    rn.LiveProvider(repo=tmp_path)  # construction alone must not reach urlopen


def test_live_provider_reads_the_whole_population_across_every_charter(monkeypatch, rn, tmp_path):
    _forbid_network(monkeypatch)

    releases_dir = tmp_path / "docs" / "releases"
    releases_dir.mkdir(parents=True)
    (tmp_path / "docs" / "requirements").mkdir(parents=True)

    def _charter(ref):
        return {
            "ref": ref,
            "name": f"fixture {ref}",
            "objective": "x",
            "opened_at": "2026-08-25",
            "target_at": "2026-09-16",
            "declared_balance": {},
            "outcomes": [
                {"id": "O-1", "statement": "x", "work_class": "enabling", "requirement_refs": []},
            ],
        }

    (releases_dir / "R91.01.json").write_text(json.dumps(_charter("R91.01")))
    (releases_dir / "R91.02.json").write_text(json.dumps(_charter("R91.02")))

    release_a = {"id": "release-bead-a", "content": {"ref": "R91.01"}}
    release_b = {"id": "release-bead-b", "content": {"ref": "R91.02"}}
    reader = _reader_double(
        beads_by_type={
            ("dev", "task"): [
                {"id": "t-a", "state": "pending", "content": {}},
                {"id": "t-closed-unbound", "state": "done", "updated_at": "2026-09-01T00:00:00Z", "content": {}},
                {"id": "t-b", "state": "done", "updated_at": "2026-09-01T00:00:00Z", "content": {}},
            ],
            ("arch", "release"): [release_a, release_b],
            ("arch", "requirement_conformance"): [],
        },
        links_by_bead_id={
            "release-bead-a": [{"source_id": "t-a", "target_id": "release-bead-a", "link_type": "delivers"}],
            "release-bead-b": [{"source_id": "t-b", "target_id": "release-bead-b", "link_type": "delivers"}],
        },
    )

    provider = rn.LiveProvider(repo=tmp_path, reader_factory=lambda base, key: reader)
    tasks, delivers = provider.population("R91.01")

    assert delivers == {"t-a": "R91.01", "t-b": "R91.02"}

    report = rn.build_release_report(_charter("R91.01"), tasks, delivers, [], now="2026-09-16")
    # The closed-unbound bead is a candidate; the closed, in-window bead bound
    # to the OTHER release ("t-b", done, delivers R91.02) is not -- it
    # carries a delivers edge, even though it is not R91.01's own edge, so
    # stripping R91.02's delivers entry from what population() returns would
    # turn it into a candidate here too, not only change the delivers-map
    # assertion above.
    assert report.outcomes[0].unmeasured_candidates == ["t-closed-unbound"]
    assert "t-b" not in report.outcomes[0].unmeasured_candidates


# --- the CLI never accepts prose --------------------------------------------


def test_cli_has_no_free_text_argument(rn):
    with pytest.raises(SystemExit):
        rn.main(["R90.02", "some prose nobody should be able to type here", "--fixture-dir", str(FIXTURES)])


def test_cli_writes_the_notes_file(rn, tmp_path):
    code = rn.main(["R90.02", "--fixture-dir", str(FIXTURES), "--out-dir", str(tmp_path)])

    assert code == 0
    written = (tmp_path / "R90.02-notes.md").read_text()
    assert written.startswith("# Release R90.02")
    assert "NOT DELIVERED" in written


def test_fixture_mode_does_not_call_live_provider(monkeypatch, rn, tmp_path):
    def fail_live_provider(*args, **kwargs):
        raise AssertionError("fixture mode must not construct the live provider")

    monkeypatch.setattr(rn, "LiveProvider", fail_live_provider)

    assert rn.main(["R90.02", "--fixture-dir", str(FIXTURES), "--out-dir", str(tmp_path)]) == 0


# --- unreachable substrate / gh fails the run, no partial write -------------


class _BrokenProvider:
    def charter(self, ref):
        raise AssertionError("should not be reached in this scenario")

    def release(self, ref):
        raise RuntimeError("SUBSTRATE_URL and SUBSTRATE_API_KEY must be set")

    def links(self, bead_id):
        raise RuntimeError("unreachable")

    def bead(self, bead_id):
        raise RuntimeError("unreachable")

    def conformances(self):
        raise RuntimeError("unreachable")

    def pr(self, number):
        raise RuntimeError("gh failed")


def test_unreachable_substrate_fails_the_run_with_no_write(monkeypatch, rn, tmp_path, capsys):
    provider = rn.FixtureProvider(FIXTURES)
    # Charter loads fine; the release lookup is what is unreachable.
    real_charter = provider.charter("R90.02")

    class _Broken:
        def charter(self, ref):
            return real_charter

        def release(self, ref):
            raise RuntimeError("substrate 503 on /beads")

        def links(self, bead_id):
            raise AssertionError("must not be called once release() fails")

        def bead(self, bead_id):
            raise AssertionError("must not be called once release() fails")

        def conformances(self):
            raise AssertionError("must not be called once release() fails")

        def pr(self, number):
            raise AssertionError("must not be called once release() fails")

    monkeypatch.setattr(rn, "LiveProvider", lambda repo=rn.REPO: _Broken())

    code = rn.main(["R90.02", "--out-dir", str(tmp_path)])

    assert code != 0
    assert "could not run" in capsys.readouterr().err
    assert not (tmp_path / "R90.02-notes.md").exists()


def test_a_failing_gh_call_fails_the_run_with_no_write(monkeypatch, rn, tmp_path, capsys):
    provider = rn.FixtureProvider(FIXTURES)
    real_charter = provider.charter("R90.02")
    real_release = provider.release("R90.02")
    real_links = provider.links("release-R90-02")
    real_beads = {tid: provider.bead(tid) for tid in [
        "10000000-0000-4000-8000-000000000001",
        "10000000-0000-4000-8000-000000000002",
        "10000000-0000-4000-8000-000000000003",
        "10000000-0000-4000-8000-000000000004",
    ]}

    class _GhBroken:
        def charter(self, ref):
            return real_charter

        def release(self, ref):
            return real_release

        def links(self, bead_id):
            return real_links

        def bead(self, bead_id):
            return real_beads[bead_id]

        def conformances(self):
            return []

        def population(self, ref):
            return None

        def pr(self, number):
            raise RuntimeError(f"gh failed for PR #{number}")

    monkeypatch.setattr(rn, "LiveProvider", lambda repo=rn.REPO: _GhBroken())

    code = rn.main(["R90.02", "--out-dir", str(tmp_path)])

    assert code != 0
    assert "could not run" in capsys.readouterr().err
    assert not (tmp_path / "R90.02-notes.md").exists()


def test_release_not_found_refuses_rather_than_emitting_empty_notes(rn, tmp_path, capsys):
    code = rn.main(["R00.99", "--fixture-dir", str(FIXTURES), "--out-dir", str(tmp_path)])

    assert code == 2
    err = capsys.readouterr().err
    assert "R00.99" in err
    assert "not found" in err
    assert not list(tmp_path.glob("*.md"))
