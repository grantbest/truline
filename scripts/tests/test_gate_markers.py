from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import re
import sys


REPO = pathlib.Path(__file__).resolve().parents[2]
DISPATCH = REPO / "apps" / "factory-dispatcher" / "dispatch.py"
UUID = "55555555-5555-4555-8555-555555555555"


def _load_markers():
    spec = importlib.util.spec_from_file_location(
        "gate_markers_under_test",
        REPO / "scripts" / "gate_markers.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_find_bead_id_reads_the_dispatcher_template_first_line():
    """The recogniser must match what open_pull_request actually writes.

    The first line is taken from dispatch.py at test time, so a template
    change that the gate tooling cannot read fails here instead of on the
    next genuine factory PR.
    """
    markers = _load_markers()
    source = DISPATCH.read_text()
    match = re.search(r'body = f"""(?P<line>.+)\n', source)
    assert match, "dispatcher PR-body template not found in dispatch.py"
    rendered = match.group("line").replace("{task['id']}", UUID)
    assert "{" not in rendered, f"unrendered placeholder in template first line: {rendered}"
    assert markers.find_bead_id(rendered) == UUID


def test_find_bead_id_accepts_explicit_line_and_plain_prose_forms():
    markers = _load_markers()
    assert markers.find_bead_id(f"Dispatched-bead id: {UUID}\n") == UUID
    assert markers.find_bead_id(f"bead id: `{UUID}`\n") == UUID
    assert markers.find_bead_id(f"Summary\n\ndev.task: {UUID}\n") == UUID
    assert markers.find_bead_id(f"Dispatched from dev.task {UUID} by codex.\n") == UUID


def test_find_bead_id_lowercases_the_id():
    markers = _load_markers()
    assert markers.find_bead_id(f"dev.task {UUID.upper()}") == UUID


def test_find_bead_id_rejects_bodies_without_a_bead():
    markers = _load_markers()
    assert markers.find_bead_id("") is None
    assert markers.find_bead_id(None) is None
    assert markers.find_bead_id("No bead here.\n\nChange kind: behavioral\n") is None


def test_malformed_bead_line_is_not_rescued_by_a_prose_reference():
    body = f"Dispatched-bead id: not-a-uuid\n\nAlso mentions dev.task {UUID}.\n"
    assert _load_markers().find_bead_id(body) is None


# --- find_release_gate_verdict: the machine-recognizable verdict record ----


def test_find_release_gate_verdict_none_when_absent():
    markers = _load_markers()
    assert markers.find_release_gate_verdict("") is None
    assert markers.find_release_gate_verdict("no verdict recorded here") is None


def test_find_release_gate_verdict_recognises_each_value():
    markers = _load_markers()
    assert markers.find_release_gate_verdict("Release-gate: MERGE") == "MERGE"
    assert (
        markers.find_release_gate_verdict("Release-gate: MERGE-WITH-CHANGES")
        == "MERGE-WITH-CHANGES"
    )
    assert markers.find_release_gate_verdict("Release-gate: DO-NOT-MERGE") == "DO-NOT-MERGE"


def test_find_release_gate_verdict_is_case_insensitive_and_bullet_tolerant():
    markers = _load_markers()
    assert markers.find_release_gate_verdict("release-gate: merge") == "MERGE"
    assert markers.find_release_gate_verdict("- Release-gate: merge-with-changes") == "MERGE-WITH-CHANGES"


def test_find_release_gate_verdict_rejects_malformed_lines():
    markers = _load_markers()
    assert markers.find_release_gate_verdict("Release-gate: MAYBE") is None
    assert markers.find_release_gate_verdict("Release-gate: MERGE please") is None
    assert markers.find_release_gate_verdict("Release-gate: MERGE-WITH-CHANGES-PLUS") is None
    assert markers.find_release_gate_verdict("Release-gate:") is None


def test_find_release_gate_verdict_a_malformed_line_is_not_rescued_elsewhere():
    markers = _load_markers()
    body = "Release-gate: MAYBE\n\nRelease-gate: MERGE\n"
    assert markers.find_release_gate_verdict(body) == "MERGE"


def test_find_release_gate_verdict_reads_pr_body_and_comments():
    markers = _load_markers()
    body = "Summary.\n\nRelease-gate: DO-NOT-MERGE\n"
    comments = ["still fixing", "Release-gate: MERGE"]
    assert markers.find_release_gate_verdict(body, comments) == "MERGE"


def test_find_release_gate_verdict_last_recorded_wins():
    """A later verdict supersedes an earlier one — the same re-verify rule CLAUDE.md
    applies to any change in what was reviewed."""
    markers = _load_markers()
    comments = ["Release-gate: MERGE", "Release-gate: DO-NOT-MERGE"]
    assert markers.find_release_gate_verdict("", comments) == "DO-NOT-MERGE"


def test_find_release_gate_verdict_handles_no_comments():
    markers = _load_markers()
    assert markers.find_release_gate_verdict("Release-gate: MERGE", None) == "MERGE"
    assert markers.find_release_gate_verdict("Release-gate: MERGE", []) == "MERGE"


# --- find_release_gate_verdict_status: order-aware staleness ---------------


def test_status_verdict_matches_find_release_gate_verdict_across_a_corpus():
    """The two must never disagree -- reimplementing the recognizer inline in
    the status function would reintroduce the #431 drift the shared module
    exists to prevent."""
    markers = _load_markers()
    corpus: list[tuple[str, list[str] | None]] = [
        ("", None),
        ("no verdict recorded here", None),
        ("Release-gate: MERGE", None),
        ("Release-gate: MERGE-WITH-CHANGES", None),
        ("Release-gate: DO-NOT-MERGE", None),
        ("release-gate: merge", None),
        ("- Release-gate: merge-with-changes", None),
        ("Release-gate: MAYBE", None),
        ("Release-gate: MAYBE\n\nRelease-gate: MERGE\n", None),
        ("Summary.\n\nRelease-gate: DO-NOT-MERGE\n", ["still fixing", "Release-gate: MERGE"]),
        ("`Release-gate: MERGE`", None),
        ("Release-gate: MERGE-WITH-CHANGES #853", None),
        ("", ["Release-gate: MERGE", "`Release-gate: DO-NOT-MERGE`"]),
        ("", ["`Release-gate: DO-NOT-MERGE`", "Release-gate: MERGE"]),
        ("Release-gate:\nMERGE\n\nRelease-gate: MAYBE\n", None),
    ]
    for body, comments in corpus:
        status = markers.find_release_gate_verdict_status(body, comments)
        assert status.verdict == markers.find_release_gate_verdict(body, comments), (body, comments)


def test_status_flags_a_later_unreadable_line_as_stale():
    """FACT AT FILING: this exact pair of comments merges today. A later
    decorated DO-NOT-MERGE must make the recognised MERGE verdict stale."""
    markers = _load_markers()
    status = markers.find_release_gate_verdict_status(
        "", ["Release-gate: MERGE", "`Release-gate: DO-NOT-MERGE`"]
    )
    assert status.verdict == "MERGE"
    assert status.stale_near_miss is not None
    assert status.stale_near_miss.line == "`Release-gate: DO-NOT-MERGE`"
    assert status.stale_near_miss.recognized_value == "DO-NOT-MERGE"


def test_status_does_not_flag_a_near_miss_recorded_before_the_winning_verdict():
    """Order decides, not presence: a near miss recorded BEFORE the last
    readable verdict must not make it stale -- the readable verdict genuinely
    supersedes it."""
    markers = _load_markers()
    status = markers.find_release_gate_verdict_status(
        "", ["`Release-gate: DO-NOT-MERGE`", "Release-gate: MERGE"]
    )
    assert status.verdict == "MERGE"
    assert status.stale_near_miss is None


def test_status_stale_near_miss_none_when_no_near_miss_exists():
    markers = _load_markers()
    status = markers.find_release_gate_verdict_status("Release-gate: MERGE")
    assert status.verdict == "MERGE"
    assert status.stale_near_miss is None


def test_status_stale_near_miss_none_when_there_is_no_verdict_at_all():
    """A near miss with no accepted verdict anywhere is the existing
    no-verdict refusal path's job, not this one's."""
    markers = _load_markers()
    status = markers.find_release_gate_verdict_status("`Release-gate: MERGE`")
    assert status.verdict is None
    assert status.stale_near_miss is None


def test_status_stale_near_miss_can_come_from_a_comment_after_a_body_verdict():
    markers = _load_markers()
    status = markers.find_release_gate_verdict_status(
        "Release-gate: MERGE", ["`Release-gate: DO-NOT-MERGE`"]
    )
    assert status.verdict == "MERGE"
    assert status.stale_near_miss is not None


def test_status_flags_a_heading_form_retraction_recorded_after_the_verdict_as_stale():
    """R1: a retraction written as a markdown heading must still make an
    earlier readable verdict stale. This is the exact defect the bead exists
    to close, reintroduced through #858's narrowing of the diagnostic-only
    _RELEASE_GATE_MENTION_RE -- the staleness path must not share that
    regex."""
    markers = _load_markers()
    status = markers.find_release_gate_verdict_status(
        "", ["Release-gate: MERGE", "## Release-gate: DO-NOT-MERGE"]
    )
    assert status.verdict == "MERGE"
    assert status.stale_near_miss is not None
    assert status.stale_near_miss.line == "## Release-gate: DO-NOT-MERGE"


def test_status_flags_a_blockquote_form_retraction_recorded_after_the_verdict_as_stale():
    """R1: same hazard, blockquote form."""
    markers = _load_markers()
    status = markers.find_release_gate_verdict_status(
        "", ["Release-gate: MERGE", "> Release-gate: DO-NOT-MERGE"]
    )
    assert status.verdict == "MERGE"
    assert status.stale_near_miss is not None
    assert status.stale_near_miss.line == "> Release-gate: DO-NOT-MERGE"


def test_status_fails_closed_when_the_verdict_spans_a_line_the_walk_cannot_place():
    """R2: RELEASE_GATE_VERDICT_RE's `\\s*` between the separator and the
    value can cross a newline in find_release_gate_verdict's whole-text
    findall, so a value on the line after "Release-gate:" is a verdict
    find_release_gate_verdict recognises. The per-line position walk here
    can never match that shape (no single line carries both the phrase and
    the value), so it must fail closed: a later mention is reported stale
    even though the winning verdict's own position could not be found."""
    markers = _load_markers()
    body = "Release-gate:\nMERGE\n\nRelease-gate: MAYBE\n"
    assert markers.find_release_gate_verdict(body) == "MERGE"
    status = markers.find_release_gate_verdict_status(body)
    assert status.verdict == "MERGE"
    assert status.stale_near_miss is not None


# --- find_release_gate_verdict_record: the optional revision line ----------


def test_record_revision_none_when_no_revision_line():
    markers = _load_markers()
    record = markers.find_release_gate_verdict_record("Release-gate: MERGE-WITH-CHANGES")
    assert record.verdict == "MERGE-WITH-CHANGES"
    assert record.revision is None


def test_record_revision_none_when_no_verdict_at_all():
    markers = _load_markers()
    record = markers.find_release_gate_verdict_record("no verdict recorded here")
    assert record.verdict is None
    assert record.revision is None


def test_record_reads_the_revision_line_immediately_after_the_verdict():
    markers = _load_markers()
    body = "Summary.\n\nRelease-gate: MERGE-WITH-CHANGES\nRelease-gate-revision: ac3584d8\n"
    record = markers.find_release_gate_verdict_record(body)
    assert record.verdict == "MERGE-WITH-CHANGES"
    assert record.revision == "ac3584d8"


def test_record_lowercases_the_revision():
    markers = _load_markers()
    body = "Release-gate: MERGE-WITH-CHANGES\nRelease-gate-revision: AC3584D8\n"
    assert markers.find_release_gate_verdict_record(body).revision == "ac3584d8"


def test_record_ignores_a_revision_line_not_immediately_adjacent():
    """A blank line, or any other line, between the verdict and the revision
    means the revision isn't attributed to it -- adjacency is required, not
    mere proximity."""
    markers = _load_markers()
    body = "Release-gate: MERGE-WITH-CHANGES\n\nRelease-gate-revision: ac3584d8\n"
    assert markers.find_release_gate_verdict_record(body).revision is None


def test_record_ignores_a_revision_line_attached_to_a_superseded_verdict():
    markers = _load_markers()
    comments = [
        "Release-gate: MERGE-WITH-CHANGES\nRelease-gate-revision: ac3584d8",
        "Release-gate: MERGE",
    ]
    record = markers.find_release_gate_verdict_record("", comments)
    assert record.verdict == "MERGE"
    assert record.revision is None


def test_record_reads_a_revision_line_from_a_comment():
    markers = _load_markers()
    comments = ["Release-gate: MERGE-WITH-CHANGES\nRelease-gate-revision: bb37e27e"]
    record = markers.find_release_gate_verdict_record("", comments)
    assert record.verdict == "MERGE-WITH-CHANGES"
    assert record.revision == "bb37e27e"


def test_record_revision_line_is_not_a_near_miss():
    """F1/F4 regression: a compliant revision line must not be read as an
    unparseable release-gate mention by either near-miss path."""
    markers = _load_markers()
    body = "Release-gate: MERGE-WITH-CHANGES\nRelease-gate-revision: ac3584d8\n"
    assert markers.find_release_gate_near_misses(body) == []
    status = markers.find_release_gate_verdict_status(body)
    assert status.verdict == "MERGE-WITH-CHANGES"
    assert status.stale_near_miss is None


def test_record_revision_line_does_not_widen_the_strict_recognizer():
    """AC-1: find_release_gate_verdict must not change for any input,
    including one that now carries an adjacent revision line."""
    markers = _load_markers()
    body = "Release-gate: MERGE-WITH-CHANGES\nRelease-gate-revision: ac3584d8\n"
    assert markers.find_release_gate_verdict(body) == "MERGE-WITH-CHANGES"


# --- revisions_match: prefix comparison, either direction ------------------


def test_revisions_match_identical():
    markers = _load_markers()
    assert markers.revisions_match("ac3584d8", "ac3584d8") is True


def test_revisions_match_abbreviated_prefix_either_direction():
    markers = _load_markers()
    assert markers.revisions_match("ac3584d8", "ac3584d8fabc0000000000000000000000000000") is True
    assert markers.revisions_match("ac3584d8fabc0000000000000000000000000000", "ac3584d8") is True


def test_revisions_match_is_case_insensitive():
    markers = _load_markers()
    assert markers.revisions_match("AC3584D8", "ac3584d8fabc0000000000000000000000000000") is True


def test_revisions_match_false_for_different_revisions():
    markers = _load_markers()
    assert markers.revisions_match("ac3584d8", "bb37e27e") is False


def test_revisions_match_false_when_either_side_is_empty():
    """An unmeasured condition must never be treated as a match."""
    markers = _load_markers()
    assert markers.revisions_match("", "ac3584d8") is False
    assert markers.revisions_match("ac3584d8", "") is False
    assert markers.revisions_match("", "") is False


# --- gate_markers.py CLI: 'revision' and 'same-revision' -------------------


def test_cli_revision_command_prints_the_recorded_revision(capsys):
    markers = _load_markers()
    body = "Release-gate: MERGE-WITH-CHANGES\nRelease-gate-revision: ac3584d8\n"
    exit_code = markers.main(
        ["revision", "--comments-json", "[]"], stdin=io.StringIO(body)
    )
    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "ac3584d8"


def test_cli_revision_command_prints_blank_when_absent(capsys):
    markers = _load_markers()
    exit_code = markers.main(
        ["revision", "--comments-json", "[]"],
        stdin=io.StringIO("Release-gate: MERGE-WITH-CHANGES"),
    )
    assert exit_code == 0
    assert capsys.readouterr().out == "\n"


def test_cli_same_revision_command_exit_codes():
    markers = _load_markers()
    assert markers.main(["same-revision", "ac3584d8", "ac3584d8fabc"]) == 0
    assert markers.main(["same-revision", "ac3584d8", "bb37e27e"]) == 1


# --- gate_markers.py CLI: the second consumer merge-pr.sh shells out to ----


def test_cli_verdict_command_delegates_to_find_release_gate_verdict(monkeypatch, capsys):
    """merge-pr.sh's only path to a verdict is this CLI. Prove it calls through to
    the single shared function rather than re-implementing recognition — the #431
    lesson applied to a shell consumer, which cannot hold a Python object reference
    the way a second Python module can."""
    markers = _load_markers()
    calls = []

    def fake(body, comments=None):
        calls.append((body, list(comments) if comments is not None else comments))
        return "MERGE-WITH-CHANGES"

    monkeypatch.setattr(markers, "find_release_gate_verdict", fake)

    exit_code = markers.main(
        ["verdict", "--comments-json", json.dumps([{"body": "a comment"}])],
        stdin=io.StringIO("the pr body"),
    )

    assert exit_code == 0
    assert calls == [("the pr body", ["a comment"])]
    assert capsys.readouterr().out.strip() == "MERGE-WITH-CHANGES"


def test_cli_verdict_command_prints_empty_line_when_no_verdict(capsys):
    markers = _load_markers()
    exit_code = markers.main(
        ["verdict", "--comments-json", "[]"], stdin=io.StringIO("no verdict here")
    )
    assert exit_code == 0
    assert capsys.readouterr().out == "\n"


def test_cli_verdict_command_accepts_ghs_comment_shape_and_plain_strings():
    markers = _load_markers()
    exit_code = markers.main(
        ["verdict", "--comments-json", json.dumps(["Release-gate: MERGE"])],
        stdin=io.StringIO(""),
    )
    assert exit_code == 0


# --- AC-1: the strict acceptance set must not widen by a single input ------


def test_strict_acceptance_set_is_pinned_literally():
    """Every accepted form returns exactly what it returns today, and every
    decorated form named in AC-2/AC-3 still returns None. This is the pin
    against the self-referential hazard: this bead must not, even
    accidentally, widen RELEASE_GATE_VERDICT_RE."""
    markers = _load_markers()

    assert markers.find_release_gate_verdict("Release-gate: MERGE") == "MERGE"
    assert (
        markers.find_release_gate_verdict("Release-gate: MERGE-WITH-CHANGES")
        == "MERGE-WITH-CHANGES"
    )
    assert markers.find_release_gate_verdict("Release-gate: DO-NOT-MERGE") == "DO-NOT-MERGE"
    assert markers.find_release_gate_verdict("- Release-gate: MERGE") == "MERGE"

    assert markers.find_release_gate_verdict("`Release-gate: MERGE`") is None
    assert markers.find_release_gate_verdict("**Release-gate: MERGE**") is None
    assert markers.find_release_gate_verdict("Release-gate: MERGE-WITH-CHANGES #853") is None
    assert (
        markers.find_release_gate_verdict(
            "`Release-gate: MERGE-WITH-CHANGES #831` - **changes complete.**"
        )
        is None
    )


# --- AC-2: the diagnostic names a near miss ---------------------------------


DECORATED_FIXTURES = [
    ("`Release-gate: MERGE`", "MERGE"),
    ("**Release-gate: MERGE**", "MERGE"),
    ("Release-gate: MERGE-WITH-CHANGES #853", "MERGE-WITH-CHANGES"),
    (
        "`Release-gate: MERGE-WITH-CHANGES #831` - **changes complete.**",
        "MERGE-WITH-CHANGES",
    ),
]


def test_near_misses_identify_all_four_decorated_fixtures():
    markers = _load_markers()
    for line, expected_value in DECORATED_FIXTURES:
        near_misses = markers.find_release_gate_near_misses(line)
        assert len(near_misses) == 1, f"expected exactly one near miss for {line!r}"
        near_miss = near_misses[0]
        assert near_miss.line == line
        assert near_miss.recognized_value == expected_value
        assert near_miss.suggested_line == f"Release-gate: {expected_value}"


def test_near_misses_scan_body_and_comments_like_find_release_gate_verdict():
    markers = _load_markers()
    body = "Summary.\n\n`Release-gate: MERGE`\n"
    comments = ["still fixing", "**Release-gate: MERGE**"]
    near_misses = markers.find_release_gate_near_misses(body, comments)
    assert [nm.line for nm in near_misses] == [
        "`Release-gate: MERGE`",
        "**Release-gate: MERGE**",
    ]


def test_near_misses_ignore_lines_that_dont_mention_a_release_gate():
    markers = _load_markers()
    assert markers.find_release_gate_near_misses("no verdict recorded here") == []
    assert markers.find_release_gate_near_misses("") == []


def test_near_misses_dont_report_a_well_formed_line():
    markers = _load_markers()
    assert markers.find_release_gate_near_misses("Release-gate: MERGE") == []


# --- AC-1/AC-3: a bare verdict word immediately followed by a revision line -


def test_near_misses_report_the_924_shape():
    """#924, measured live: the outer loop recorded the verdict as a bare
    word with the revision line immediately after it.
    RELEASE_GATE_VERDICT_RE's required `release[-_ ]gate` prefix had nothing
    to match, so the verdict was read as no verdict at all. The near-miss
    diagnostic must catch this shape."""
    markers = _load_markers()
    body = (
        "MERGE-WITH-CHANGES\n"
        "Release-gate-revision: 15307c6ccd1490e3840cf8e1bebde775a60be51f\n"
    )
    near_misses = markers.find_release_gate_near_misses(body)
    assert len(near_misses) == 1
    near_miss = near_misses[0]
    assert near_miss.line == "MERGE-WITH-CHANGES"
    assert near_miss.recognized_value == "MERGE-WITH-CHANGES"
    assert near_miss.suggested_line == "Release-gate: MERGE-WITH-CHANGES"


def test_near_misses_ignore_a_bare_verdict_word_inside_a_worker_report_fence():
    """GATE FINDING (#939): a factory PR body is not wholly human-authored.

    dispatch.py:3277 interpolates the worker's own stdout verbatim into a
    ``` fence. merge-pr.sh prints near misses beside its no-verdict refusal,
    so reporting a bare verdict word found in that fence tells the operator a
    verdict was "nearly" recorded and hands them the exact line to write --
    sourced from the very thing being judged. Measured before the fix: this
    body reported `found: MERGE / read as: MERGE / required: Release-gate:
    MERGE`. It must report nothing.
    """
    markers = _load_markers()
    body = (
        "Dispatched by the factory from `dev.task` 1234abcd\n"
        "\n"
        "## Dispatcher verification\n"
        "```\n"
        "MERGE\n"
        "Release-gate-revision: ac3584d8\n"
        "```\n"
    )
    assert markers.find_release_gate_near_misses(body) == []


def test_a_tilde_line_inside_a_backtick_fence_does_not_close_it():
    """GATE FINDING F1 (#951): fences close only with the marker that opened them.

    A single shared `in_fence` boolean lets a ``~~~`` line inside a ``` fence
    close it, which re-opens the exact worker-stdout false positive #939 shut.
    Not hypothetical: dispatch.py interpolates the worker's stdout verbatim into
    a ``` fence, and a run of ``~~~~`` is ordinary separator output.

    Measured before the fix -- shared boolean reported 1 near miss on this body
    (line='MERGE') while main reported 0.
    """
    markers = _load_markers()
    body = (
        "Dispatched by the factory from `dev.task` abcd1234\n"
        "\n"
        "## Dispatcher verification\n"
        "```\n"
        "pytest -q\n"
        "~~~~~~~~~~~~~~~~~~~~\n"
        "MERGE\n"
        "Release-gate-revision: ac3584d8\n"
        "```\n"
    )
    assert markers.find_release_gate_near_misses(body) == []


def test_a_backtick_line_inside_a_tilde_fence_does_not_close_it():
    """The mirror of the case above, so the fix is symmetric rather than
    special-cased for the marker that happened to be reported."""
    markers = _load_markers()
    body = (
        "~~~\n"
        "worker output\n"
        "```\n"
        "MERGE\n"
        "Release-gate-revision: ac3584d8\n"
        "~~~\n"
    )
    assert markers.find_release_gate_near_misses(body) == []


def test_near_misses_still_report_a_bare_word_after_a_fence_has_closed():
    """The control for the test above: fence tracking must not swallow the
    real case. The same shape OUTSIDE the fence is still a near miss, so the
    fix narrows the diagnostic to worker-authored regions rather than
    disabling it."""
    markers = _load_markers()
    body = (
        "## Dispatcher verification\n"
        "```\n"
        "pytest -q -> 41 passed\n"
        "```\n"
        "MERGE\n"
        "Release-gate-revision: ac3584d8\n"
    )
    near_misses = markers.find_release_gate_near_misses(body)
    assert len(near_misses) == 1
    assert near_misses[0].recognized_value == "MERGE"
    assert near_misses[0].suggested_line == "Release-gate: MERGE"


def test_near_misses_report_a_bare_word_adjacent_to_a_revision_line_for_each_legal_verdict():
    markers = _load_markers()
    for word in ["MERGE", "MERGE-WITH-CHANGES", "DO-NOT-MERGE"]:
        body = f"{word}\nRelease-gate-revision: ac3584d8\n"
        near_misses = markers.find_release_gate_near_misses(body)
        assert len(near_misses) == 1, f"expected a near miss for {word!r}"
        assert near_misses[0].recognized_value == word
        assert near_misses[0].suggested_line == f"Release-gate: {word}"


def test_near_misses_ignore_a_bare_verdict_word_with_no_revision_line_after_it():
    """AC-3: a bare word with nothing after it is ordinary prose, not a near
    miss -- reporting on the word alone would make this diagnostic noise
    that gets ignored."""
    markers = _load_markers()
    assert markers.find_release_gate_near_misses("MERGE-WITH-CHANGES\n") == []
    assert markers.find_release_gate_near_misses("MERGE") == []


def test_near_misses_ignore_a_bare_verdict_word_separated_from_the_revision_line_by_a_blank_line():
    """AC-3: adjacency is the whole mechanism (#910) -- a blank line between
    the bare word and the revision line means the pairing that identifies
    the shape isn't present, so this must not be reported."""
    markers = _load_markers()
    body = "MERGE-WITH-CHANGES\n\nRelease-gate-revision: ac3584d8\n"
    assert markers.find_release_gate_near_misses(body) == []


def test_near_misses_bare_word_shape_still_yields_no_strict_verdict_or_record():
    """AC-2: this bead is a diagnostic only. The malformed #924 shape must
    still fail to parse as a verdict -- proven here by asserting both
    directions for the same input: the near miss fires AND
    find_release_gate_verdict / find_release_gate_verdict_record still
    report no verdict, so merge-pr.sh still refuses it."""
    markers = _load_markers()
    body = (
        "MERGE-WITH-CHANGES\n"
        "Release-gate-revision: 15307c6ccd1490e3840cf8e1bebde775a60be51f\n"
    )
    assert len(markers.find_release_gate_near_misses(body)) == 1
    assert markers.find_release_gate_verdict(body) is None
    record = markers.find_release_gate_verdict_record(body)
    assert record.verdict is None
    assert record.revision is None


# --- AC-3: the diagnostic never invents a verdict ---------------------------


ILLEGAL_VALUE_FIXTURES = [
    "Release-gate: LGTM",
    "Release-gate: MERGE IF CI GOES GREEN",
]


def test_near_misses_report_no_value_for_illegal_verdicts():
    markers = _load_markers()
    for line in ILLEGAL_VALUE_FIXTURES:
        near_misses = markers.find_release_gate_near_misses(line)
        assert len(near_misses) == 1, f"expected exactly one near miss for {line!r}"
        near_miss = near_misses[0]
        assert near_miss.recognized_value is None
        assert near_miss.suggested_line is None


HYPHEN_SUFFIXED_ILLEGAL_VALUE_FIXTURES = [
    "Release-gate: DO-NOT-MERGE-YET",
    "Release-gate: MERGE-WITH-CHANGES-PENDING-CI",
]


def test_near_misses_never_guess_a_verdict_for_a_hyphen_suffixed_illegal_value():
    """A legal value is a strict prefix of these illegal ones. The diagnostic
    must not report the prefix as the recognised value -- that is exactly the
    guessing AC-3 forbids, just with a hyphen standing in for whitespace."""
    markers = _load_markers()
    for line in HYPHEN_SUFFIXED_ILLEGAL_VALUE_FIXTURES:
        near_misses = markers.find_release_gate_near_misses(line)
        assert len(near_misses) == 1, f"expected exactly one near miss for {line!r}"
        near_miss = near_misses[0]
        assert near_miss.recognized_value is None
        assert near_miss.suggested_line is None


def test_near_miss_diagnostic_is_never_accepted_as_a_verdict():
    """A near-miss report is a diagnostic for a person, never an input to a
    merge decision: the original line must still be unrecognisable to the
    strict path, whether or not the near-miss diagnostic recovered a value
    from it."""
    markers = _load_markers()
    for line, _ in DECORATED_FIXTURES:
        assert markers.find_release_gate_verdict(line) is None
    for line in ILLEGAL_VALUE_FIXTURES:
        assert markers.find_release_gate_verdict(line) is None
    for line in HYPHEN_SUFFIXED_ILLEGAL_VALUE_FIXTURES:
        assert markers.find_release_gate_verdict(line) is None


# --- the diagnostic matches the strict recognizer's case-insensitivity -----


def test_near_miss_matches_strict_recognizers_case_insensitivity():
    """The strict path accepts `release-gate: merge` once decoration is
    stripped (it is case-insensitive); the diagnostic must recognise the
    same value from the decorated form instead of reporting a false
    'no recognised value'."""
    markers = _load_markers()
    near_misses = markers.find_release_gate_near_misses("`release-gate: merge`")
    assert len(near_misses) == 1
    near_miss = near_misses[0]
    assert near_miss.recognized_value == "MERGE"
    assert near_miss.suggested_line == "Release-gate: MERGE"


# --- the mention must be anchored, so prose stops triggering it ------------


def test_near_misses_ignore_a_mid_sentence_mention():
    markers = _load_markers()
    assert markers.find_release_gate_near_misses(
        "This bead fixes the release-gate near-miss diagnostic."
    ) == []
    assert markers.find_release_gate_near_misses(
        "See the release-gate: MERGE example above."
    ) == []


def test_near_misses_still_fire_on_anchored_mentions_with_allowed_decoration():
    markers = _load_markers()
    for line in [
        "Release-gate: LGTM",
        "- Release-gate: LGTM",
        "* Release-gate: LGTM",
        "  Release-gate: LGTM",
        "`Release-gate: LGTM`",
        "**Release-gate: LGTM**",
    ]:
        assert len(markers.find_release_gate_near_misses(line)) == 1, line


# --- AC-5: writer-side check, exposed through the CLI -----------------------


def test_cli_check_command_exits_zero_for_a_well_formed_verdict():
    markers = _load_markers()
    exit_code = markers.main(["check"], stdin=io.StringIO("Release-gate: MERGE"))
    assert exit_code == 0


def test_cli_check_command_exits_nonzero_and_prints_near_miss_diagnostic(capsys):
    markers = _load_markers()
    exit_code = markers.main(
        ["check"], stdin=io.StringIO("`Release-gate: MERGE-WITH-CHANGES #831`")
    )
    assert exit_code != 0
    err = capsys.readouterr().err
    assert "`Release-gate: MERGE-WITH-CHANGES #831`" in err
    assert "Release-gate: MERGE-WITH-CHANGES" in err


def test_cli_check_command_exits_nonzero_with_no_guessed_value_for_illegal_verdict(capsys):
    markers = _load_markers()
    exit_code = markers.main(
        ["check"], stdin=io.StringIO("Release-gate: MERGE IF CI GOES GREEN")
    )
    assert exit_code != 0
    err = capsys.readouterr().err
    assert "no recognised release-gate verdict value" in err


def test_cli_check_command_exits_nonzero_when_no_release_gate_mention(capsys):
    markers = _load_markers()
    exit_code = markers.main(["check"], stdin=io.StringIO("just fixing a typo"))
    assert exit_code != 0
    err = capsys.readouterr().err
    assert "Release-gate: MERGE" in err


def test_cli_check_command_validates_candidate_as_the_comment_it_documents():
    markers = _load_markers()
    candidate = (
        "Release-gate: MERGE\n"
        "dev.task 11111111-1111-1111-1111-111111111111 is now gated.\n"
    )
    exit_code = markers.main(["check"], stdin=io.StringIO(candidate))
    assert exit_code == 0


def test_cli_check_command_validates_candidate_as_the_comment_bead_line_form():
    markers = _load_markers()
    candidate = (
        "Dispatched-bead id: 11111111-1111-1111-1111-111111111111\n"
        "Release-gate: MERGE\n"
    )
    exit_code = markers.main(["check"], stdin=io.StringIO(candidate))
    assert exit_code == 0


def test_cli_check_command_reads_from_file(tmp_path):
    markers = _load_markers()
    candidate = tmp_path / "comment.txt"
    candidate.write_text("Release-gate: DO-NOT-MERGE")
    exit_code = markers.main(["check", "--file", str(candidate)], stdin=io.StringIO(""))
    assert exit_code == 0


def test_cli_stale_near_miss_command_exits_zero_and_silent_when_undisputed():
    markers = _load_markers()
    exit_code = markers.main(
        ["stale-near-miss", "--comments-json", "[]"],
        stdin=io.StringIO("Release-gate: MERGE"),
    )
    assert exit_code == 0


def test_cli_stale_near_miss_command_exits_nonzero_and_prints_diagnostic_when_stale(capsys):
    markers = _load_markers()
    comments_json = json.dumps(["Release-gate: MERGE", "`Release-gate: DO-NOT-MERGE`"])
    exit_code = markers.main(
        ["stale-near-miss", "--comments-json", comments_json],
        stdin=io.StringIO(""),
    )
    assert exit_code != 0
    out = capsys.readouterr().out
    assert "`Release-gate: DO-NOT-MERGE`" in out
    assert "Release-gate: DO-NOT-MERGE" in out


def test_cli_near_misses_command_prints_formatted_diagnostic(capsys):
    markers = _load_markers()
    exit_code = markers.main(
        ["near-misses", "--comments-json", "[]"],
        stdin=io.StringIO("`Release-gate: MERGE`"),
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "`Release-gate: MERGE`" in out


# --- AC-1/AC-2: the bare-verdict detector must not fire on a quoted example -

# Committed verbatim from PR #939's own body (fetched via `gh pr view 939
# --json body`), the minimal excerpt that reproduces the measured false
# positive: explaining the #924 defect by quoting its exact malformed shape,
# indented under a paragraph, is prose *about* the defect, not a fresh
# instance of it. Before this bead's fix, find_release_gate_near_misses
# reported one near miss for this text (line="    MERGE-WITH-CHANGES",
# recognized_value="MERGE-WITH-CHANGES") -- verified by running it against
# unpatched scripts/gate_markers.py (AC-5). A synthetic example would not
# have proven the real shape is covered.
PR_939_INDENTED_QUOTE_EXCERPT = (
    "The outer loop recorded #924's gate verdict as a bare verdict word on its own line, with the\n"
    "revision line immediately after it:\n"
    "\n"
    "    MERGE-WITH-CHANGES\n"
    "    Release-gate-revision: 15307c6ccd1490e3840cf8e1bebde775a60be51f\n"
)


def test_near_misses_ignore_the_939_body_indented_quotation_of_the_924_shape():
    """AC-2: #939's own body is the headline fixture. It must go from firing
    (measured against the live PR) to silent."""
    markers = _load_markers()
    assert markers.find_release_gate_near_misses(PR_939_INDENTED_QUOTE_EXCERPT) == []


def test_near_misses_still_report_the_924_shape_unindented():
    """AC-3, the control for the test above: the identical shape, NOT quoted
    inside an indented block, is the real #924 near miss and must still be
    reported with the same recognized_value and suggested_line -- a change
    that silenced everything would satisfy AC-1 while destroying AC-3."""
    markers = _load_markers()
    body = (
        "MERGE-WITH-CHANGES\n"
        "Release-gate-revision: 15307c6ccd1490e3840cf8e1bebde775a60be51f\n"
    )
    near_misses = markers.find_release_gate_near_misses(body)
    assert len(near_misses) == 1
    assert near_misses[0].line == "MERGE-WITH-CHANGES"
    assert near_misses[0].recognized_value == "MERGE-WITH-CHANGES"
    assert near_misses[0].suggested_line == "Release-gate: MERGE-WITH-CHANGES"


def test_near_misses_ignore_a_bare_verdict_word_inside_an_indented_code_block():
    """AC-1: a synthetic, minimal case of the same indentation rule, so the
    behaviour isn't pinned only to the exact #939 wording."""
    markers = _load_markers()
    body = (
        "Some paragraph:\n"
        "\n"
        "    MERGE\n"
        "    Release-gate-revision: ac3584d8\n"
    )
    assert markers.find_release_gate_near_misses(body) == []


def test_near_misses_ignore_a_bare_verdict_word_inside_a_tilde_fence():
    """AC-1: ~~~ is the other CommonMark fence marker and gets the same
    treatment as ```."""
    markers = _load_markers()
    body = (
        "## Dispatcher verification\n"
        "~~~\n"
        "MERGE\n"
        "Release-gate-revision: ac3584d8\n"
        "~~~\n"
    )
    assert markers.find_release_gate_near_misses(body) == []


def test_near_misses_still_report_a_bare_word_after_a_tilde_fence_has_closed():
    """The control for the test above, mirroring the ``` control already in
    place: the same shape OUTSIDE the fence is still a near miss."""
    markers = _load_markers()
    body = (
        "## Dispatcher verification\n"
        "~~~\n"
        "pytest -q -> 41 passed\n"
        "~~~\n"
        "MERGE\n"
        "Release-gate-revision: ac3584d8\n"
    )
    near_misses = markers.find_release_gate_near_misses(body)
    assert len(near_misses) == 1
    assert near_misses[0].recognized_value == "MERGE"


def test_near_misses_ignore_a_bare_verdict_word_in_a_blockquote():
    """AC-1's control case: a blockquote already does not fire, and must
    keep not firing -- #939's gate verified this before this bead existed,
    so no production change is needed for it, only this pinning test."""
    markers = _load_markers()
    body = "> MERGE\n> Release-gate-revision: ac3584d8\n"
    assert markers.find_release_gate_near_misses(body) == []


# --- dev.finding 807baee1, AC-1: a well-formed verdict line inside a quoted
# --- region is never a verdict, for any of the three strict readers --------


def test_finding_807baee1_reproduction_fenced_worker_report_yields_no_verdict():
    """The finding's own reproduction: a dispatcher-shaped body whose
    worker-report fence holds a bare, well-formed `Release-gate: MERGE`
    line. Before this fix this returned MERGE; it must return no verdict."""
    markers = _load_markers()
    body = (
        "Dispatched by the factory from `dev.task` "
        f"[`{UUID}`]({UUID}).\n"
        "\n"
        "## Dispatcher verification\n"
        "```\n"
        "Release-gate: MERGE\n"
        "```\n"
    )
    assert markers.find_release_gate_verdict(body) is None


def test_verdict_inside_a_backtick_fence_is_not_read():
    markers = _load_markers()
    body = "```\nRelease-gate: MERGE\n```\n"
    assert markers.find_release_gate_verdict(body) is None


def test_verdict_inside_a_tilde_fence_opened_inside_a_backtick_fence_is_not_read():
    """A ~~~ fence opened INSIDE an already-open ``` fence does not close the
    outer fence (CommonMark: a fence closes only with the marker that opened
    it) -- the verdict stays inside the outer, still-open ``` fence either
    way, so it must not be read."""
    markers = _load_markers()
    body = "```\nworker output\n~~~\nRelease-gate: MERGE\n~~~\n```\n"
    assert markers.find_release_gate_verdict(body) is None


def test_verdict_outside_a_fence_after_it_closes_is_still_read():
    """Control for the two tests above: the same line OUTSIDE any fence is a
    real verdict and must still be recognised -- a fix that over-masks would
    pass the tests above by accident."""
    markers = _load_markers()
    body = "```\nworker output\n```\nRelease-gate: MERGE\n"
    assert markers.find_release_gate_verdict(body) == "MERGE"


def test_verdict_inside_an_indented_code_block_is_not_read():
    """Before this fix, RELEASE_GATE_VERDICT_RE's leading `\\s*` absorbed
    4-space indentation, so a quoted example indented under a paragraph
    (the #939 idiom) read as a genuine verdict."""
    markers = _load_markers()
    body = "Some paragraph:\n\n    Release-gate: MERGE\n"
    assert markers.find_release_gate_verdict(body) is None


def test_verdict_record_and_status_also_skip_a_fenced_verdict():
    markers = _load_markers()
    body = "```\nRelease-gate: MERGE\nRelease-gate-revision: ac3584d8\n```\n"
    assert markers.find_release_gate_verdict_record(body) == markers.ReleaseGateVerdictRecord(
        verdict=None, revision=None
    )
    status = markers.find_release_gate_verdict_status(body)
    assert status.verdict is None
    assert status.stale_near_miss is None


def test_near_misses_bare_branch_still_uses_the_shared_quoted_definition():
    """The near-miss bare-verdict branch is wired through the same
    _quoted_mask the strict readers use -- proven by the existing fence/
    indent fixtures above continuing to report nothing, pinned again here
    for the specific shared-helper claim."""
    markers = _load_markers()
    body = "```\nMERGE\nRelease-gate-revision: ac3584d8\n```\n"
    assert markers.find_release_gate_near_misses(body) == []


# --- dev.finding 807baee1, AC-2: a dispatcher-shaped body is never read for
# --- a verdict -- only comments count -------------------------------------


def test_body_verdict_on_a_bead_declaring_pr_with_no_comments_is_no_verdict():
    markers = _load_markers()
    body = f"Dispatched-bead id: {UUID}\n\nRelease-gate: MERGE\n"
    assert markers.find_release_gate_verdict(body) is None
    assert markers.find_release_gate_verdict_record(body).verdict is None
    assert markers.find_release_gate_verdict_status(body).verdict is None


def test_body_verdict_on_a_bead_declaring_pr_is_superseded_by_a_later_comment():
    markers = _load_markers()
    body = f"Dispatched-bead id: {UUID}\n\nRelease-gate: MERGE\n"
    comments = ["Release-gate: DO-NOT-MERGE"]
    assert markers.find_release_gate_verdict(body, comments) == "DO-NOT-MERGE"
    record = markers.find_release_gate_verdict_record(body, comments)
    assert record.verdict == "DO-NOT-MERGE"


def test_body_verdict_on_an_attended_pr_with_no_bead_id_is_unchanged():
    markers = _load_markers()
    body = "Summary.\n\nRelease-gate: MERGE\n"
    assert markers.find_release_gate_verdict(body) == "MERGE"
    record = markers.find_release_gate_verdict_record(body)
    assert record.verdict == "MERGE"


def test_body_verdict_with_revision_on_a_bead_declaring_pr_is_ignored_even_record():
    """A well-formed verdict AND adjacent revision line, both in the body of
    a bead-declaring PR, must be fully ignored -- not just the verdict."""
    markers = _load_markers()
    body = (
        f"Dispatched-bead id: {UUID}\n\n"
        "Release-gate: MERGE\n"
        "Release-gate-revision: ac3584d8\n"
    )
    record = markers.find_release_gate_verdict_record(body)
    assert record.verdict is None
    assert record.revision is None


def test_malformed_bead_line_does_not_trigger_the_body_exclusion():
    """find_bead_id rejects a malformed bead-id line outright (it is never
    rescued by a prose reference); AC-2's body exclusion keys off the same
    function, so a malformed bead line must not suppress a real body
    verdict on what is, for this purpose, still an unattended body with no
    resolvable bead."""
    markers = _load_markers()
    body = "Dispatched-bead id: not-a-uuid\n\nRelease-gate: MERGE\n"
    assert markers.find_bead_id(body) is None
    assert markers.find_release_gate_verdict(body) == "MERGE"
