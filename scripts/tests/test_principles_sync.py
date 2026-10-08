"""Tests for the `arch.principle` registry sync tool.

No test here opens a network connection or a database. `push` is exercised
against `FakePrincipleStore`, an in-memory double — never `principles_sync.Substrate`,
which `main()` only constructs for the `push` subcommand and which reads
`SUBSTRATE_URL`/`SUBSTRATE_API_KEY` from the environment. `push --apply` against
the real substrate is an operator action run after this change merges, not
something this suite performs.

The golden-file test (see `test_golden_*` below) deliberately does not carry a
hand-typed fixture reproducing all twelve statements verbatim — a transcription
typo in a fixture is indistinguishable from a real parser regression. Instead
it cross-checks `parse_registry`'s output against a second, independently
written extraction routine over the same real
`docs/architecture/principles.md`, plus low-risk structural assertions (ids,
per-id status, which ids carry extra note bullets). Two independently coded
readers of one file silently agreeing is strong evidence neither lost or
altered a field.
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import principles_sync as ps  # noqa: E402

REGISTRY_PATH = REPO / "docs" / "architecture" / "principles.md"


# ---------------------------------------------------------------------------
# parse — golden-file cross-check against the real registry
# ---------------------------------------------------------------------------


def _independent_reference_parse(text: str) -> dict[str, dict[str, str]]:
    """A deliberately different extraction: non-greedy span to the next
    bullet, rather than the real parser's line-by-line continuation state
    machine. Used only to cross-check parse_registry against the same file —
    not itself the contract."""
    blocks = re.split(r"\n(?=### PRIN-\d{3} )", text)
    out: dict[str, dict[str, str]] = {}
    for block in blocks:
        heading = re.match(r"### (PRIN-\d{3}) — (.+)", block)
        if not heading:
            continue
        entry_id = heading.group(1)

        def field(label: str) -> str | None:
            match = re.search(rf"- \*\*{label}:\*\* (.+?)(?=\n- \*\*|\Z)", block, re.S)
            if not match:
                return None
            return re.sub(r"\s+", " ", match.group(1)).strip()

        out[entry_id] = {
            "name": heading.group(2).strip(),
            "statement": field("Statement"),
            "source": field("Source"),
        }
    return out


EXPECTED_STATUS = {
    "PRIN-001": "adopted",
    "PRIN-002": "proposed",
    "PRIN-003": "adopted",
    "PRIN-004": "adopted",
    "PRIN-005": "adopted",
    "PRIN-006": "adopted",
    "PRIN-007": "adopted",
    "PRIN-008": "enforced",
    "PRIN-009": "adopted",
    "PRIN-010": "proposed",
    "PRIN-011": "adopted",
    "PRIN-012": "proposed",
    "PRIN-013": "proposed",
    "PRIN-014": "adopted",
    "PRIN-015": "proposed",
    "PRIN-016": "proposed",
    "PRIN-017": "proposed",
    "PRIN-018": "proposed",
    "PRIN-019": "proposed",
    "PRIN-020": "proposed",
    "PRIN-021": "proposed",
}
#: Non-History extra bullets. Since 2026-09-07 every entry carries a History
#: bullet (the dated-transition record), so "has notes at all" stopped being
#: the discriminator; the golden now pins which entries carry OTHER labels.
EXPECTED_IDS_WITH_NON_HISTORY_NOTES = {"PRIN-001", "PRIN-003"}


def test_golden_parse_covers_expected_principles_in_order():
    # Derived from EXPECTED_STATUS rather than hardcoded, per the #448/#449
    # gate finding: a fixed count turns every registry addition into a
    # cross-PR break instead of a one-dict edit.
    text = REGISTRY_PATH.read_text()
    registry = ps.parse_registry(text)
    assert [entry.id for entry in registry.entries] == [
        f"PRIN-{n:03d}" for n in range(1, len(EXPECTED_STATUS) + 1)
    ]


def test_golden_parse_status_matches_the_reviewed_file():
    text = REGISTRY_PATH.read_text()
    registry = ps.parse_registry(text)
    for entry in registry.entries:
        assert entry.status == EXPECTED_STATUS[entry.id], entry.id


def test_golden_parse_notes_presence_matches_the_reviewed_file():
    text = REGISTRY_PATH.read_text()
    registry = ps.parse_registry(text)
    # Every entry carries a History bullet (2026-09-07)...
    ids_with_history = {
        entry.id for entry in registry.entries
        if any(n.startswith("History: ") for n in entry.notes)
    }
    assert ids_with_history == set(EXPECTED_STATUS)
    # ...and the non-History extras stay pinned.
    ids_with_other_notes = {
        entry.id
        for entry in registry.entries
        if any(not n.startswith("History: ") for n in entry.notes)
    }
    assert ids_with_other_notes == EXPECTED_IDS_WITH_NON_HISTORY_NOTES


def test_golden_parse_agrees_with_an_independent_extraction():
    text = REGISTRY_PATH.read_text()
    registry = ps.parse_registry(text)
    reference = _independent_reference_parse(text)
    assert set(reference) == {entry.id for entry in registry.entries}
    for entry in registry.entries:
        expected = reference[entry.id]
        assert entry.name == expected["name"], entry.id
        assert entry.statement == expected["statement"], entry.id
        assert entry.source == expected["source"], entry.id


def test_parse_rejects_a_missing_statement_bullet():
    text = (
        "Seeded 2026-08-17. Change by PR only.\n\n---\n\n"
        "### PRIN-099 — Missing statement\n\n"
        "- **Source:** somewhere\n"
        "- **Status:** `proposed` — because\n"
    )
    with pytest.raises(ValueError, match="missing a Statement bullet"):
        ps.parse_registry(text)


def test_parse_rejects_an_unknown_status_value():
    text = (
        "Seeded 2026-08-17. Change by PR only.\n\n---\n\n"
        "### PRIN-099 — Bad status\n\n"
        "- **Statement:** s\n"
        "- **Source:** s\n"
        "- **Status:** `wontfix` — nope\n"
    )
    with pytest.raises(ValueError, match="not one of"):
        ps.parse_registry(text)


# ---------------------------------------------------------------------------
# render — round-trip
# ---------------------------------------------------------------------------


def test_render_round_trips_through_parse_on_the_real_registry():
    text = REGISTRY_PATH.read_text()
    first = ps.parse_registry(text)
    rendered = ps.render_registry(first)
    second = ps.parse_registry(rendered)
    assert first.entries == second.entries


def test_render_round_trips_on_a_minimal_registry():
    text = (
        "Seeded 2026-08-17. Change by PR only.\n\n---\n\n"
        "### PRIN-001 — A title\n\n"
        "- **Statement:** Wraps across\n"
        "  two lines like the real file.\n"
        "- **Source:** DRv2 p. 1.\n"
        "- **Status:** `proposed` — not yet.\n"
        "- **Enforcement gap:** nothing mechanized.\n"
    )
    first = ps.parse_registry(text)
    rendered = ps.render_registry(first)
    second = ps.parse_registry(rendered)
    assert first == second
    assert second.entries[0].statement == "Wraps across two lines like the real file."
    assert second.entries[0].notes == (
        "Enforcement gap: nothing mechanized.",
    )


# ---------------------------------------------------------------------------
# push — dry-run, idempotent apply, all against an in-memory double
# ---------------------------------------------------------------------------


class FakePrincipleStore:
    """The `push` side of the substrate contract, in memory.

    `create`/`patch` validate through the same `validate_principle_content`
    the real `ArchPrincipleContent` schema enforces, instead of accepting
    anything — the FakeSubstrate.add_note lesson (CLAUDE.md operating rules):
    a double that accepts more than the live contract is a second
    implementation.
    """

    def __init__(self) -> None:
        self._beads: dict[str, dict] = {}
        self._next_id = 1
        self.create_calls: list[dict] = []
        self.patch_calls: list[tuple[str, dict]] = []

    def list_principles(self, limit: int = 1000) -> list[dict]:
        return [dict(bead, content=dict(bead["content"])) for bead in self._beads.values()]

    def create(self, content: dict) -> dict:
        ps.validate_principle_content(content)
        bead_id = f"bead-{self._next_id}"
        self._next_id += 1
        bead = {"id": bead_id, "content": dict(content)}
        self._beads[bead_id] = bead
        self.create_calls.append(content)
        return bead

    def patch(self, bead_id: str, content: dict) -> dict:
        ps.validate_principle_content(content)
        self._beads[bead_id]["content"] = dict(content)
        self.patch_calls.append((bead_id, content))
        return self._beads[bead_id]


@pytest.fixture()
def registry() -> ps.Registry:
    return ps.parse_registry(REGISTRY_PATH.read_text())


def test_push_dry_run_prints_a_create_plan_and_writes_nothing(registry):
    store = FakePrincipleStore()
    plan = ps.plan_push(store, registry, dry_run=True)

    assert plan.created == [entry.id for entry in registry.entries]
    assert plan.updated == []
    assert store.create_calls == []
    assert store.list_principles() == []

    report = ps.report_plan(plan)
    assert "DRY RUN" in report
    for entry in registry.entries:
        assert entry.id in report


def test_push_apply_creates_one_bead_per_principle(registry):
    store = FakePrincipleStore()
    plan = ps.plan_push(store, registry, dry_run=False)

    assert plan.created == [entry.id for entry in registry.entries]
    assert len(store.list_principles()) == len(registry.entries)
    refs = {bead["content"]["ref"] for bead in store.list_principles()}
    assert refs == {entry.id for entry in registry.entries}


def test_push_apply_twice_is_a_noop():
    """Idempotent on external id: a re-push updates rather than duplicates."""
    text = REGISTRY_PATH.read_text()
    registry = ps.parse_registry(text)
    store = FakePrincipleStore()

    first = ps.plan_push(store, registry, dry_run=False)
    assert len(first.created) == len(registry.entries)

    second = ps.plan_push(store, ps.parse_registry(text), dry_run=False)
    assert second.created == []
    assert second.updated == []
    assert len(second.unchanged) == len(registry.entries)
    assert len(store.list_principles()) == len(registry.entries)
    assert store.patch_calls == []


def test_push_apply_updates_when_the_registry_changes(registry):
    store = FakePrincipleStore()
    ps.plan_push(store, registry, dry_run=False)

    changed_entries = tuple(
        entry if entry.id != "PRIN-001" else _with_statement(entry, "A revised statement.")
        for entry in registry.entries
    )
    changed_registry = ps.Registry(header=registry.header, entries=changed_entries)

    plan = ps.plan_push(store, changed_registry, dry_run=False)
    assert plan.created == []
    assert plan.updated == ["PRIN-001"]
    assert len(plan.unchanged) == len(registry.entries) - 1
    assert len(store.patch_calls) == 1


def _with_statement(entry: ps.PrincipleEntry, statement: str) -> ps.PrincipleEntry:
    return ps.PrincipleEntry(
        id=entry.id,
        name=entry.name,
        statement=statement,
        source=entry.source,
        status=entry.status,
        status_note=entry.status_note,
        notes=entry.notes,
    )


def test_push_status_history_is_stable_across_runs_and_carries_a_reason(registry):
    store = FakePrincipleStore()
    ps.plan_push(store, registry, dry_run=False)
    first_history = {
        bead["content"]["ref"]: bead["content"]["status_history"]
        for bead in store.list_principles()
    }

    ps.plan_push(store, ps.parse_registry(REGISTRY_PATH.read_text()), dry_run=False)
    second_history = {
        bead["content"]["ref"]: bead["content"]["status_history"]
        for bead in store.list_principles()
    }

    assert first_history == second_history
    # Since 2026-09-07 the pushed history is the file's parsed History bullet,
    # not a synthesized single-entry seed: lengths vary (PRIN-007/014 carry
    # two transitions), every item carries a non-empty reason, and each
    # bead's history must equal what the file declares.
    registry = ps.parse_registry(REGISTRY_PATH.read_text())
    declared = {e.id: ps.parse_status_history(e) for e in registry.entries}
    for ref, history in first_history.items():
        assert history == declared[ref], ref
        assert all(item["reason"].strip() for item in history), ref


# ---------------------------------------------------------------------------
# the double SHALL reject what the schema rejects
# ---------------------------------------------------------------------------


def _valid_content() -> dict:
    return ps.content_for(
        ps.PrincipleEntry(
            id="PRIN-999",
            name="A test principle",
            statement="A statement.",
            source="A source.",
            status="proposed",
            status_note="because.",
            notes=(),
        ),
        seeded_date="2026-08-17",
    )


def test_validate_accepts_well_formed_content():
    ps.validate_principle_content(_valid_content())  # must not raise


def test_validate_rejects_an_unknown_status():
    content = _valid_content()
    content["status"] = "wontfix"
    with pytest.raises(ps.PrincipleValidationError, match="status"):
        ps.validate_principle_content(content)


def test_validate_rejects_status_history_missing_a_reason():
    content = _valid_content()
    content["status_history"] = [{"date": "2026-08-17", "status": "proposed"}]
    with pytest.raises(ps.PrincipleValidationError, match="reason"):
        ps.validate_principle_content(content)


def test_validate_rejects_status_history_with_a_blank_reason():
    content = _valid_content()
    content["status_history"] = [
        {"date": "2026-08-17", "status": "proposed", "reason": "   "}
    ]
    with pytest.raises(ps.PrincipleValidationError, match="reason"):
        ps.validate_principle_content(content)


def test_validate_rejects_status_history_with_an_unknown_status():
    content = _valid_content()
    content["status_history"] = [
        {"date": "2026-08-17", "status": "wontfix", "reason": "because"}
    ]
    with pytest.raises(ps.PrincipleValidationError):
        ps.validate_principle_content(content)


def test_validate_rejects_status_history_with_an_unparseable_date():
    content = _valid_content()
    content["status_history"] = [
        {"date": "not-a-date", "status": "proposed", "reason": "because"}
    ]
    with pytest.raises(ps.PrincipleValidationError, match="date"):
        ps.validate_principle_content(content)


@pytest.mark.parametrize("field_name", ["statement", "rationale", "source"])
def test_validate_rejects_a_blank_required_field(field_name):
    content = _valid_content()
    content[field_name] = "   "
    with pytest.raises(ps.PrincipleValidationError):
        ps.validate_principle_content(content)


@pytest.mark.parametrize("key", sorted(ps.MEASUREMENT_KEYS))
def test_validate_rejects_embedded_measurement_fields(key):
    content = _valid_content()
    content[key] = "2026-08-18"
    with pytest.raises(ps.PrincipleValidationError, match="measurement"):
        ps.validate_principle_content(content)


def test_validate_rejects_a_nested_measurement_field():
    content = _valid_content()
    content["status_history"][0]["conformance"] = "green"
    with pytest.raises(ps.PrincipleValidationError, match="measurement"):
        ps.validate_principle_content(content)


def test_fake_store_create_raises_on_invalid_content_instead_of_accepting_it():
    store = FakePrincipleStore()
    bad = _valid_content()
    bad["status"] = "wontfix"
    with pytest.raises(ps.PrincipleValidationError):
        store.create(bad)
    assert store.list_principles() == []


# ---------------------------------------------------------------------------
# meta-test — the double rejects what the live schema rejects
# ---------------------------------------------------------------------------

_SCHEMA_CASES = [
    ("valid", lambda c: c, True),
    ("unknown status", lambda c: {**c, "status": "wontfix"}, False),
    (
        "status_history missing reason",
        lambda c: {**c, "status_history": [{"date": "2026-08-17", "status": "proposed"}]},
        False,
    ),
    (
        "status_history blank reason",
        lambda c: {
            **c,
            "status_history": [
                {"date": "2026-08-17", "status": "proposed", "reason": "  "}
            ],
        },
        False,
    ),
    ("blank statement", lambda c: {**c, "statement": ""}, False),
    ("blank rationale", lambda c: {**c, "rationale": "  "}, False),
    ("blank source", lambda c: {**c, "source": ""}, False),
    ("embedded verdict key", lambda c: {**c, "verdict": "pass"}, False),
]


# ---------------------------------------------------------------------------
# fetch — reads back the same structured form parse() produces
# ---------------------------------------------------------------------------


def test_fetch_matches_parse_field_for_field(registry):
    store = FakePrincipleStore()
    ps.plan_push(store, registry, dry_run=False)

    fetched = ps.fetch_entries(store)

    assert fetched == registry.entries


def test_fetch_skips_beads_with_no_ref():
    store = FakePrincipleStore()
    store._beads["stray"] = {"id": "stray", "content": {"statement": "no ref here"}}

    assert ps.fetch_entries(store) == ()


def test_fetch_sorts_by_id_regardless_of_store_order(registry):
    store = FakePrincipleStore()
    ps.plan_push(store, registry, dry_run=False)
    # Simulate an out-of-order store by reversing the dict.
    store._beads = dict(reversed(list(store._beads.items())))

    fetched = ps.fetch_entries(store)

    assert [entry.id for entry in fetched] == sorted(entry.id for entry in fetched)


# ---------------------------------------------------------------------------
# check-view — is the file still the beads' view?
# ---------------------------------------------------------------------------


def test_check_view_passes_when_the_file_matches_the_beads(registry):
    store = FakePrincipleStore()
    ps.plan_push(store, registry, dry_run=False)

    ok, diffs = ps.check_view(store, REGISTRY_PATH.read_text())

    assert ok is True
    assert diffs == []


def test_check_view_fails_and_names_the_id_and_field_on_a_status_drift(registry):
    store = FakePrincipleStore()
    ps.plan_push(store, registry, dry_run=False)

    bead = next(
        b for b in store.list_principles() if b["content"]["ref"] == "PRIN-005"
    )
    drifted = dict(bead["content"])
    drifted["status"] = "enforced"
    drifted["status_history"] = drifted["status_history"] + [
        {"date": "2026-08-19", "status": "enforced", "reason": "test drift"}
    ]
    store.patch(bead["id"], drifted)

    ok, diffs = ps.check_view(store, REGISTRY_PATH.read_text())

    assert ok is False
    assert any("PRIN-005" in line and "status" in line for line in diffs)


def test_check_view_fails_when_the_beads_have_an_id_the_file_lacks(registry):
    store = FakePrincipleStore()
    ps.plan_push(store, registry, dry_run=False)
    extra = ps.content_for(
        ps.PrincipleEntry(
            id="PRIN-999",
            name="Not in the file",
            statement="A statement.",
            source="A source.",
            status="proposed",
        ),
        seeded_date="2026-08-19",
    )
    store.create(extra)

    ok, diffs = ps.check_view(store, REGISTRY_PATH.read_text())

    assert ok is False
    assert any("PRIN-999" in line for line in diffs)


def test_check_view_fails_when_the_file_has_an_id_the_beads_lack(registry):
    store = FakePrincipleStore()
    trimmed = ps.Registry(header=registry.header, entries=registry.entries[1:])
    ps.plan_push(store, trimmed, dry_run=False)

    ok, diffs = ps.check_view(store, REGISTRY_PATH.read_text())

    assert ok is False
    assert any(registry.entries[0].id in line for line in diffs)


# ---------------------------------------------------------------------------
# propose-promotion — drafts a transition, writes nothing
# ---------------------------------------------------------------------------


def test_propose_promotion_drafts_a_dated_transition_and_a_diff(registry):
    before = REGISTRY_PATH.read_text()

    promotion = ps.propose_promotion(
        registry, "PRIN-002", "adopted", reason="Mechanized by P1/P5.", today="2026-08-19"
    )

    assert promotion.id == "PRIN-002"
    assert promotion.from_status == "proposed"
    assert promotion.to_status == "adopted"
    assert promotion.status_history_entry == {
        "date": "2026-08-19",
        "status": "adopted",
        "reason": "Mechanized by P1/P5.",
    }
    assert "-- **Status:** `proposed`" in promotion.diff
    assert "+- **Status:** `adopted`" in promotion.diff
    assert "PRIN-002" in promotion.diff
    assert REGISTRY_PATH.read_text() == before  # nothing was written


def test_propose_promotion_without_a_reason_inserts_a_placeholder(registry):
    promotion = ps.propose_promotion(registry, "PRIN-002", "adopted", today="2026-08-19")

    assert promotion.status_history_entry["reason"] == ps.REASON_PLACEHOLDER
    assert ps.REASON_PLACEHOLDER in promotion.diff


def test_propose_promotion_refuses_an_unknown_id(registry):
    with pytest.raises(ps.PromotionError, match="unknown principle id"):
        ps.propose_promotion(registry, "PRIN-999", "adopted", today="2026-08-19")


def test_propose_promotion_refuses_an_unknown_status(registry):
    with pytest.raises(ps.PromotionError, match="not a known status"):
        ps.propose_promotion(registry, "PRIN-002", "wontfix", today="2026-08-19")


def test_propose_promotion_refuses_a_no_op_transition(registry):
    with pytest.raises(ps.PromotionError, match="already"):
        ps.propose_promotion(registry, "PRIN-001", "adopted", today="2026-08-19")


def test_report_promotion_is_ready_to_paste_into_a_pr(registry):
    promotion = ps.propose_promotion(
        registry, "PRIN-002", "adopted", reason="Mechanized.", today="2026-08-19"
    )
    report = ps.report_promotion(promotion)

    assert "PRIN-002" in report
    assert "proposed -> adopted" in report
    assert "2026-08-19" in report
    assert "--- principles.md" in report
    assert "+++ principles.md" in report


def test_double_matches_the_live_schemas_module_rejections():
    """Proves the double rejects what the schema task's acceptance criteria
    reject, by running the same payloads through both."""
    pydantic = pytest.importorskip("pydantic")
    schema_path = REPO / "apps" / "substrate" / "src" / "schemas.py"
    if not schema_path.exists():
        pytest.skip(f"{schema_path} not present in this checkout")

    spec = importlib.util.spec_from_file_location("_substrate_schemas_under_test", schema_path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"could not import the live schema module: {exc}")

    ArchPrincipleContent = module.ArchPrincipleContent
    base = _valid_content()

    for label, mutate, should_be_valid in _SCHEMA_CASES:
        content = mutate(dict(base))

        try:
            ArchPrincipleContent(**content)
            live_accepts = True
        except pydantic.ValidationError:
            live_accepts = False

        try:
            ps.validate_principle_content(content)
            double_accepts = True
        except ps.PrincipleValidationError:
            double_accepts = False

        assert live_accepts == should_be_valid, f"live schema verdict for {label!r}"
        assert double_accepts == should_be_valid, f"double verdict for {label!r}"
        assert live_accepts == double_accepts, (
            f"double diverges from the live schema for {label!r}: "
            f"live={live_accepts} double={double_accepts}"
        )


# --- status_history: representable, round-tripped, never flattened (2026-09-07) ---


def _entry_with_history(status="adopted"):
    return ps.PrincipleEntry(
        id="PRIN-098",
        name="A test principle",
        statement="Things SHALL be tested.",
        source="Test fixture.",
        status=status,
        notes=(
            "History: 2026-08-17 `proposed` — seeded (#425); "
            "2026-09-03 `adopted` — promoted on the D2 ladder",
        ),
    )


def test_history_bullet_parses_into_structured_transitions():
    history = ps.parse_status_history(_entry_with_history())
    assert history == [
        {"date": "2026-08-17", "status": "proposed", "reason": "seeded (#425)"},
        {"date": "2026-09-03", "status": "adopted", "reason": "promoted on the D2 ladder"},
    ]


def test_content_for_uses_the_parsed_history_not_the_synthesized_seed():
    """The flattening regression this mechanism ends: push previously wrote a
    one-entry synthesized history over any real bead-side record."""
    content = ps.content_for(_entry_with_history(), seeded_date="2026-08-17")
    assert len(content["status_history"]) == 2
    assert content["status_history"][1]["status"] == "adopted"


def test_entry_without_history_keeps_the_synthesized_seed():
    entry = ps.PrincipleEntry(
        id="PRIN-099", name="n", statement="s", source="src", status="proposed"
    )
    content = ps.content_for(entry, seeded_date="2026-08-17")
    assert len(content["status_history"]) == 1
    assert content["status_history"][0]["date"] == "2026-08-17"


def test_malformed_history_fails_loudly_rather_than_flattening():
    entry = ps.PrincipleEntry(
        id="PRIN-097", name="n", statement="s", source="src", status="adopted",
        notes=("History: sometime last week it got adopted",),
    )
    with pytest.raises(ValueError, match="does not parse"):
        ps.parse_status_history(entry)


def test_history_round_trips_through_render_and_parse():
    registry = ps.Registry(
        header="# Principles\n\nSeeded 2026-08-17.", entries=(_entry_with_history(),)
    )
    rendered = ps.render_registry(registry)
    reparsed = ps.parse_registry(rendered)
    assert ps.parse_status_history(reparsed.entries[0]) == [
        {"date": "2026-08-17", "status": "proposed", "reason": "seeded (#425)"},
        {"date": "2026-09-03", "status": "adopted", "reason": "promoted on the D2 ladder"},
    ]


def test_promotion_appends_to_an_existing_history_bullet():
    registry = ps.Registry(
        header="# Principles\n\nSeeded 2026-08-17.", entries=(_entry_with_history(),)
    )
    promotion = ps.propose_promotion(
        registry, "PRIN-098", "enforced", reason="mechanized by the invariant", today="2026-09-07"
    )
    assert "2026-09-07 `enforced` — mechanized by the invariant" in promotion.diff
    assert promotion.diff.count("History:") >= 1


def test_promotion_creates_a_history_bullet_when_none_exists():
    entry = ps.PrincipleEntry(
        id="PRIN-096", name="n", statement="s", source="src", status="proposed"
    )
    registry = ps.Registry(
        header="# Principles\n\nSeeded 2026-08-17.", entries=(entry,)
    )
    promotion = ps.propose_promotion(
        registry, "PRIN-096", "adopted", reason="evidence landed", today="2026-09-07"
    )
    assert "+- **History:** 2026-09-07 `adopted` — evidence landed" in promotion.diff


def test_check_view_catches_history_drift():
    """History rides notes, and notes is in _ENTRY_DIFF_FIELDS — a bead whose
    history diverges from the file must be named by check-view."""
    file_entry = _entry_with_history()
    registry = ps.Registry(
        header="# Principles\n\nSeeded 2026-08-17.", entries=(file_entry,)
    )
    drifted = ps.content_for(file_entry, "2026-08-17")
    drifted["notes"] = ["History: 2026-08-17 `proposed` — seeded (#425)"]

    class OneBeadStore:
        def list_principles(self):
            return [{"content": drifted}]

    ok, diffs = ps.check_view(
        OneBeadStore(), ps.render_registry(registry)
    )
    assert not ok
    assert any("PRIN-098" in d for d in diffs)


def test_promotion_refuses_a_semicolon_reason_rather_than_wedging_the_registry():
    """';' is the History item separator; a reason carrying one would draft a
    bullet the parser rejects — the write path must not violate its own read
    grammar (release-gate finding)."""
    registry = ps.Registry(
        header="# Principles\n\nSeeded 2026-08-17.", entries=(_entry_with_history(),)
    )
    with pytest.raises(ps.PromotionError, match="may not contain ';'"):
        ps.propose_promotion(
            registry, "PRIN-098", "enforced",
            reason="mechanized; gate wired", today="2026-09-07",
        )
