"""Citation discipline for the source_class KEEP ratification (c9cebed5, D-C).

`docs/architecture/ea-metamodel.md` §8.5 records the KEEP decision and cites
per-writer/per-type counts from the committed snapshot
`tests/fixtures/source_class_ratification_2026-09-24.json` (#1021). Nothing
enforced those numbers staying tied to the fixture they claim to cite --
exactly the drift `scripts/readme_conformance.py` exists to catch for
README's own counts (PRIN-005). This module ties them the same way: every
number named in §8.5 is asserted equal to the fixture's own value, so an
edit to either one that breaks the citation fails the suite instead of
rotting quietly.

No substrate client is imported here and no network call is made -- the
worker reads the committed snapshot only, per D7 (CLAUDE.md 2026-09-13) and
dispatch.py's own rendering of it ("D7 GRANTS NO EXEMPTION").
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_DISPATCHER_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _DISPATCHER_ROOT.parents[1]

FIXTURE_PATH = (
    _DISPATCHER_ROOT / "tests" / "fixtures" / "source_class_ratification_2026-09-24.json"
)
METAMODEL_PATH = _REPO_ROOT / "docs" / "architecture" / "ea-metamodel.md"

SECTION_HEADING = "### 8.5 `source_class: derived` on the 2026-09-16 sweep"


def _load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text())


def _load_section() -> str:
    text = METAMODEL_PATH.read_text()
    assert SECTION_HEADING in text, (
        "ea-metamodel.md no longer carries the §8.5 KEEP ratification heading this test cites"
    )
    start = text.index(SECTION_HEADING)
    return text[start:]


def _load_paragraph(marker: str) -> str:
    """Return the blank-line-delimited paragraph of §8.5 that starts with
    `marker`, so an assertion can be anchored to the sentence it claims to
    test instead of matching anywhere in the whole section (which the totals
    line and writer table would otherwise satisfy for free).
    """
    section = _load_section()
    paragraphs = section.split("\n\n")
    matches = [p for p in paragraphs if p.lstrip().startswith(marker)]
    assert len(matches) == 1, (
        f"expected exactly one §8.5 paragraph starting with {marker!r}, found {len(matches)}"
    )
    return matches[0]


def _int_from_thousands(token: str) -> int:
    return int(token.replace(",", ""))


def test_fixture_declares_the_keep_decision():
    fixture = _load_fixture()
    assert fixture["decision"] == "KEEP -- ratified 2026-09-24"


def test_section_names_the_keep_decision():
    section = _load_section()
    assert "**Decision: KEEP.**" in section


def test_writer_table_matches_fixture_derived_by_created_by():
    fixture = _load_fixture()
    section = _load_section()
    writer_row_re = re.compile(r"\| `([^`]+)` \| ([\d,]+) \|")
    cited = {
        writer: _int_from_thousands(count)
        for writer, count in writer_row_re.findall(section)
    }
    assert cited == fixture["derived_by_created_by"]


def test_by_type_prose_matches_fixture_derived_by_type():
    fixture = _load_fixture()
    section = _load_section()
    type_re = re.compile(r"([\d,]+) `(\w+)`")
    by_type_line = next(
        line for line in section.splitlines() if line.startswith("By type:")
    )
    cited = {
        kind: _int_from_thousands(count) for count, kind in type_re.findall(by_type_line)
    }
    assert cited == fixture["derived_by_type"]


def test_totals_match_fixture():
    fixture = _load_fixture()
    section = _load_section()
    totals = fixture["totals"]
    by_class = totals["by_source_class"]
    totals_line = next(
        line for line in section.splitlines() if line.startswith("Total `arch.*` population")
    )
    stripped = totals_line.replace(",", "")
    assert str(totals["arch_beads"]) in stripped
    assert str(by_class["derived"]) in stripped
    assert str(by_class["observed"]) in stripped
    assert str(by_class["authored"]) in stripped
    assert str(by_class["<unset>"]) in stripped


def test_sweep_size_figures_are_each_attributed_to_a_source():
    # §8.5 names two different sweep-size figures (1,129 and ~1,015). Each
    # must be attributed to the bead/note it came from, and the section must
    # say why they don't conflict (different scope), not just place them next
    # to each other and let the reader infer it.
    section = _load_section()
    assert "1,129" in section
    assert "~1,015" in section
    assert "`c9cebed5` intent records a 2026-09-17 measurement" in section
    assert "note `e140fc10`'s **~1,015" in section
    assert "does not conflict with the 1,129 above" in section


def test_writer_table_is_distinguished_from_the_ratified_set():
    # The writer table is the whole `derived` population at the 2026-09-24
    # measurement (it matches the fixture exactly, per
    # test_writer_table_matches_fixture_derived_by_created_by), not the set
    # this decision ratifies -- it also holds rows the sweep never touched.
    # That distinction must be stated, not left implicit.
    normalized = " ".join(_load_section().split())
    assert "not the ratified set" in normalized
    assert "requirements-load` rows, enrolled for `derived` only since 2026-09-08" in normalized


def test_observed_is_the_stable_control():
    fixture = _load_fixture()
    paragraph = _load_paragraph("**Control: `observed` did not move.**")
    assert fixture["totals"]["by_source_class"]["observed"] == 167
    assert fixture["prior_observation_2026_09_17"]["observed"] == 167
    # Both the 2026-09-17 and 2026-09-24 readings of `observed` must appear
    # inside this specific paragraph -- not merely somewhere in §8.5, where
    # the totals line's own "167 `observed`" would satisfy a bare substring
    # check regardless of what this paragraph says.
    assert paragraph.count("167") >= 2


def test_authored_discrepancy_is_named_but_not_investigated():
    fixture = _load_fixture()
    paragraph = _load_paragraph("**`authored` is a bare, unexplained fact here.**")
    current_authored = fixture["totals"]["by_source_class"]["authored"]
    prior_authored = fixture["prior_observation_2026_09_17"]["authored"]
    assert current_authored == 14
    assert prior_authored == 29
    assert str(current_authored) in paragraph
    assert str(prior_authored) in paragraph
    assert "dev.finding" in paragraph
    assert "does not investigate" in paragraph


def test_ea_load_arch_service_rows_are_carved_out_of_the_ratification():
    fixture = _load_fixture()
    section = _load_section()
    assert fixture["arch_service_derived"] == {"by_writer": {"ea-load": 6}, "count": 6}
    assert "is not part of this ratification" in section
    assert "`ea-load`" in section


def test_unset_96_rows_named_with_2026_09_17_figures_not_the_fixtures_188():
    fixture = _load_fixture()
    section = _load_section()
    # The fixture's own unset count (188, 2026-09-24) is a superset that
    # includes writers/types never part of the disputed 96 -- the record
    # must cite the original 2026-09-17 figures, not this larger one.
    assert fixture["totals"]["by_source_class"]["<unset>"] == 188
    assert "32 `arch.application`" in section
    assert "63 `arch.capability`" in section
    assert "1 `arch.incident`" in section
    assert "95 written by `ea-load`, 1 by `claude:outer-loop`" in section


def test_no_revert_path_is_claimed_or_built():
    section = _load_section()
    assert "No revert path exists and none was built" in section
    revert_tool_paths = [
        p
        for p in (_DISPATCHER_ROOT / "activities").glob("*revert*")
    ]
    assert revert_tool_paths == []
