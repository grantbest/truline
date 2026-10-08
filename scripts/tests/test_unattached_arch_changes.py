"""unattached_arch_changes.py -- finding 3's 19-of-65, made askable.

Also covers finding 2's own warning: a per-bead traversal that reads
`GET /beads/{id}/links` with the endpoint's `direction=both` default
double-counts any edge whose both endpoints share a namespace. The live-fetch
path here must ask for `direction=outgoing` explicitly on every request; a
fake client asserts the literal query string it was called with, not just the
result it returns.
"""

from __future__ import annotations

import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import unattached_arch_changes as sut  # noqa: E402


CHANGE_WITH_APPLICATION = {"id": "change-1"}
CHANGE_WITHOUT_APPLICATION = {"id": "change-2"}
APPLICATION_ID = "app-1"


# --- find_unattached_changes ----------------------------------------------


def test_a_change_with_an_affects_edge_to_an_application_is_not_unattached():
    changes = [CHANGE_WITH_APPLICATION]
    links = [
        {"source_id": "change-1", "target_id": APPLICATION_ID, "link_type": "affects"}
    ]
    assert sut.find_unattached_changes(changes, links, [APPLICATION_ID]) == []


def test_a_change_with_no_affects_edge_at_all_is_unattached():
    changes = [CHANGE_WITHOUT_APPLICATION]
    assert sut.find_unattached_changes(changes, [], [APPLICATION_ID]) == ["change-2"]


def test_a_change_whose_affects_edge_targets_a_non_application_is_unattached():
    """affects to a capability, say, does not satisfy 'reaches an application'."""
    changes = [CHANGE_WITHOUT_APPLICATION]
    links = [
        {"source_id": "change-2", "target_id": "capability-1", "link_type": "affects"}
    ]
    assert sut.find_unattached_changes(changes, links, [APPLICATION_ID]) == ["change-2"]


def test_a_non_affects_edge_to_an_application_does_not_count():
    """`depends_on` reaching an application is not the same claim `affects` makes."""
    changes = [CHANGE_WITHOUT_APPLICATION]
    links = [
        {"source_id": "change-2", "target_id": APPLICATION_ID, "link_type": "depends_on"}
    ]
    assert sut.find_unattached_changes(changes, links, [APPLICATION_ID]) == ["change-2"]


def test_nineteen_of_sixty_five_shaped_population():
    changes = [{"id": f"attached-{i}"} for i in range(46)] + [
        {"id": f"unattached-{i}"} for i in range(19)
    ]
    links = [
        {"source_id": f"attached-{i}", "target_id": APPLICATION_ID, "link_type": "affects"}
        for i in range(46)
    ]
    unattached = sut.find_unattached_changes(changes, links, [APPLICATION_ID])
    assert len(unattached) == 19
    assert len(changes) == 65


# --- report formatting ------------------------------------------------------


def test_format_report_names_every_unattached_change():
    report = sut.format_report(["change-2"], changes_checked=2)
    assert "change-2" in report
    assert "Reaching no arch.application via 'affects': 1" in report


def test_format_report_on_a_fully_attached_population_names_none():
    report = sut.format_report([], changes_checked=3)
    assert "Reaching no arch.application via 'affects': 0" in report


# --- live fetch: direction stated explicitly --------------------------------


class _FakeReader:
    """Records every path it was asked to GET, so the test can assert the
    literal query string -- not just the shape of the result."""

    def __init__(self, responses: dict[str, list[dict]]):
        self.responses = responses
        self.requested_paths: list[str] = []

    def get(self, path: str):
        self.requested_paths.append(path)
        for prefix, body in self.responses.items():
            if path.startswith(prefix):
                return body
        return []


def test_live_fetch_asks_for_outgoing_direction_explicitly_not_the_default():
    reader = _FakeReader(
        {
            "/beads?namespace=arch&type=change": [{"id": "change-1"}],
            "/beads?namespace=arch&type=application": [{"id": APPLICATION_ID}],
            "/beads/change-1/links": [
                {"source_id": "change-1", "target_id": APPLICATION_ID, "link_type": "affects"}
            ],
        }
    )

    snapshot = sut._fetch_live_snapshot(reader)

    link_requests = [p for p in reader.requested_paths if "/links" in p]
    assert link_requests, "expected at least one links request"
    for path in link_requests:
        assert "direction=outgoing" in path, (
            f"{path!r} did not state direction=outgoing explicitly -- the "
            "endpoint's direction=both default double-counts edges between "
            "same-namespace beads (finding 2)"
        )
        assert "link_type=affects" in path

    unattached = sut.find_unattached_changes(
        snapshot["changes"], snapshot["links"], snapshot["applications"]
    )
    assert unattached == []


def test_live_fetch_surfaces_an_unattached_change():
    reader = _FakeReader(
        {
            "/beads?namespace=arch&type=change": [{"id": "change-2"}],
            "/beads?namespace=arch&type=application": [{"id": APPLICATION_ID}],
            "/beads/change-2/links": [],
        }
    )

    snapshot = sut._fetch_live_snapshot(reader)
    unattached = sut.find_unattached_changes(
        snapshot["changes"], snapshot["links"], snapshot["applications"]
    )

    assert unattached == ["change-2"]


# --- CLI: report, never gate -------------------------------------------------


def _snapshot_file(tmp_path, changes, links, applications):
    path = tmp_path / "snapshot.json"
    path.write_text(
        json.dumps({"changes": changes, "links": links, "applications": applications})
    )
    return path


def test_main_returns_zero_even_when_changes_are_unattached(tmp_path, capsys):
    path = _snapshot_file(tmp_path, [CHANGE_WITHOUT_APPLICATION], [], [APPLICATION_ID])

    exit_code = sut.main([str(path)])

    assert exit_code == 0
    assert "Reaching no arch.application via 'affects': 1" in capsys.readouterr().out


def test_main_returns_zero_on_a_fully_attached_population(tmp_path, capsys):
    path = _snapshot_file(
        tmp_path,
        [CHANGE_WITH_APPLICATION],
        [{"source_id": "change-1", "target_id": APPLICATION_ID, "link_type": "affects"}],
        [APPLICATION_ID],
    )

    exit_code = sut.main([str(path)])

    assert exit_code == 0
    assert "Reaching no arch.application via 'affects': 0" in capsys.readouterr().out


def test_main_returns_nonzero_when_it_cannot_run(tmp_path):
    path = tmp_path / "not-json.txt"
    path.write_text("{this is not valid json")

    assert sut.main([str(path)]) == 2


def test_main_returns_nonzero_when_snapshot_is_not_an_object(tmp_path):
    path = tmp_path / "list.json"
    path.write_text(json.dumps([1, 2, 3]))

    assert sut.main([str(path)]) == 2
