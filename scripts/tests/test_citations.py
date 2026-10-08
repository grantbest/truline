"""The `PRIN-` citation checker.

`docs/architecture/principles.md` is the registry; every doc that cites a
`PRIN-NNN` id is making a traceability claim. This is the meta-test class
`lint.yml`'s "Test the checkers and the guard meta-tests" step exists for: a gate
that has quietly stopped being able to fail is indistinguishable from one that
passes everything, so `test_the_checker_fails_on_a_dangling_citation` below feeds
it a fixture that MUST be rejected.
"""

from __future__ import annotations

import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import citations as ck  # noqa: E402


def _write(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _registry(tmp_path: pathlib.Path, ids: list[str]) -> None:
    # Each heading carries its id exactly once, so a test can reason about
    # `citations_checked` counts without also counting the registry's own
    # self-citations twice per entry.
    body = "\n\n".join(f"### {i} — A principle\n\n- **Statement:** s." for i in ids)
    _write(tmp_path / ck.REGISTRY_PATH, f"# Principle Registry\n\n---\n\n{body}\n")


# --- resolving vs. dangling ---------------------------------------------------


def test_a_citation_that_resolves_is_not_dangling(tmp_path):
    _registry(tmp_path, ["PRIN-001"])
    _write(tmp_path / "docs/plans/example.md", "Applies: PRIN-001.\n")

    report = ck.audit(tmp_path)

    assert report.citations_checked == 2  # the registry's own heading + this one
    assert report.dangling == []


def test_a_citation_that_does_not_resolve_is_dangling_with_file_and_line(tmp_path):
    _registry(tmp_path, ["PRIN-001"])
    _write(tmp_path / "docs/plans/example.md", "Intro line.\nApplies: PRIN-999.\n")

    report = ck.audit(tmp_path)

    assert len(report.dangling) == 1
    dangling = report.dangling[0]
    assert dangling.path == tmp_path / "docs/plans/example.md"
    assert dangling.line == 2
    assert dangling.ref == "PRIN-999"


def test_a_missing_registry_is_reported_as_unreachable_not_as_dangling_citations(tmp_path):
    """PRIN-015: a fault must announce itself with its cause attached. An
    unreachable registry is an environment fault, not evidence that every
    real citation in the tree is wrong — it must not populate `dangling`."""
    _write(tmp_path / "docs/plans/example.md", "Applies: PRIN-001.\n")

    report = ck.audit(tmp_path)

    assert report.scan_error is not None
    assert report.dangling == []


def test_a_missing_registry_still_fails_closed(tmp_path):
    """Only the diagnosis changes; the refusal does not (PRIN-015). An
    unreachable registry must still exit non-zero — exiting 0 here would be
    strictly worse than the defect being fixed."""
    _write(tmp_path / "docs/plans/example.md", "Applies: PRIN-001.\n")

    code = ck.main(["--repo", str(tmp_path)])

    assert code != 0


def test_format_report_names_registry_unreachable_rather_than_reporting_zero_dangling(tmp_path):
    _write(tmp_path / "docs/plans/example.md", "Applies: PRIN-001.\n")

    report = ck.audit(tmp_path)
    text = ck.format_report(report, tmp_path)

    assert "Dangling" not in text
    assert "registry unreachable" in text
    assert "cannot evaluate" in text


def test_a_present_registry_is_unaffected_by_the_registry_unavailable_path(tmp_path):
    """Control for the two tests above: with the registry present, behaviour
    is exactly what it was before — no scan_error, and a non-zero citation
    count so this cannot pass vacuously."""
    _registry(tmp_path, ["PRIN-001"])
    _write(tmp_path / "docs/plans/example.md", "Applies: PRIN-001.\n")

    report = ck.audit(tmp_path)

    assert report.scan_error is None
    assert report.citations_checked > 0
    assert report.dangling == []


def test_multiple_citations_on_one_line_are_each_checked(tmp_path):
    _registry(tmp_path, ["PRIN-001"])
    _write(tmp_path / "docs/plans/example.md", "Applies: PRIN-001, PRIN-002, PRIN-999.\n")

    report = ck.audit(tmp_path)

    assert report.citations_checked == 4  # the registry's own heading + 3 here
    assert sorted(c.ref for c in report.dangling) == ["PRIN-002", "PRIN-999"]


def test_a_four_digit_run_is_not_mistaken_for_a_three_digit_id(tmp_path):
    _registry(tmp_path, ["PRIN-001"])
    _write(tmp_path / "docs/plans/example.md", "Not a real id: PRIN-0013.\n")

    report = ck.audit(tmp_path)

    assert report.citations_checked == 1  # only the registry's own heading


# --- fenced code blocks are ignored, matching the change-kind awk -------------


def test_a_citation_inside_a_fenced_code_block_is_ignored(tmp_path):
    _registry(tmp_path, ["PRIN-001"])
    _write(
        tmp_path / "docs/plans/example.md",
        "\n".join(["Real citation: PRIN-001.", "```", "Example: PRIN-999.", "```", ""]),
    )

    report = ck.audit(tmp_path)

    assert report.citations_checked == 2  # the registry's own heading + the real one
    assert report.dangling == []


def test_a_citation_on_the_line_reopening_after_a_fence_is_still_checked(tmp_path):
    _registry(tmp_path, ["PRIN-001"])
    _write(
        tmp_path / "docs/plans/example.md",
        "\n".join(["```", "fenced PRIN-999", "```", "Applies: PRIN-999."]),
    )

    report = ck.audit(tmp_path)

    assert report.citations_checked == 2  # the registry's own heading + the real one
    assert [c.ref for c in report.dangling] == ["PRIN-999"]


# --- only the scanned directories are read -------------------------------------


def test_files_outside_the_scanned_directories_are_not_read(tmp_path):
    _registry(tmp_path, ["PRIN-001"])
    _write(tmp_path / "docs/reference/notes.md", "PRIN-999 is unchecked here.\n")

    report = ck.audit(tmp_path)

    assert report.files_scanned == 1  # only principles.md itself
    assert report.citations_checked == 1  # the id in principles.md's own heading


def test_citations_are_found_under_both_scanned_directories(tmp_path):
    _registry(tmp_path, ["PRIN-001"])
    _write(tmp_path / "docs/architecture/other.md", "PRIN-001 here.\n")
    _write(tmp_path / "docs/plans/nested/deep.md", "PRIN-001 there.\n")

    report = ck.audit(tmp_path)

    assert report.dangling == []
    assert report.citations_checked == 3  # heading + the two body citations


# --- the meta-test: the gate must still be able to fail -----------------------


def test_the_checker_fails_on_a_dangling_citation(tmp_path):
    """A fixture that MUST be rejected. If this ever passes, the checker has
    quietly stopped being able to fail."""
    _registry(tmp_path, ["PRIN-001"])
    _write(tmp_path / "docs/plans/broken.md", "This cites PRIN-999, which does not exist.\n")

    code = ck.main(["--repo", str(tmp_path)])

    assert code != 0


def test_a_clean_tree_passes(tmp_path):
    _registry(tmp_path, ["PRIN-001"])
    _write(tmp_path / "docs/plans/fine.md", "This cites PRIN-001, which exists.\n")

    code = ck.main(["--repo", str(tmp_path)])

    assert code == 0


def test_the_report_names_the_dangling_citation(tmp_path, capsys):
    _registry(tmp_path, ["PRIN-001"])
    _write(tmp_path / "docs/plans/broken.md", "Cites PRIN-999.\n")

    ck.main(["--repo", str(tmp_path)])

    out = capsys.readouterr().out
    assert "PRIN-999" in out
    assert "broken.md" in out


# --- the live repository -------------------------------------------------------


def test_the_live_repository_has_no_dangling_citations():
    """The whole point: the ids this repo actually cites today all resolve."""
    report = ck.audit(REPO)

    assert report.citations_checked > 0
    assert report.dangling == [], ck.format_report(report, REPO)
