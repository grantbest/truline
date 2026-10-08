from __future__ import annotations

import importlib.util
import json
import pathlib
import sys


REPO = pathlib.Path(__file__).resolve().parents[2]
MODULE_PATH = REPO / "scripts" / "unattended-merge-metrics.py"

BEAD_ID = "55555555-5555-4555-8555-555555555555"


def _load_metrics():
    spec = importlib.util.spec_from_file_location("unattended_merge_metrics_under_test", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _record(metrics, **overrides):
    data = {
        "pr_number": 1,
        "merge_commit": "aaaaaaa1",
        "merged_at": "2026-08-01T00:00:00Z",
        "body": f"Dispatched-bead id: {BEAD_ID}\nRelease-gate: MERGE\n",
        "merged_by": {"login": "factory-bot", "type": "Bot"},
        "autonomy": "auto",
    }
    data.update(overrides)
    return metrics.record_from_dict(data)


def test_bot_merged_dev_task_pr_is_counted_unattended(capsys):
    metrics = _load_metrics()
    record = _record(metrics)

    report = metrics.compute_metrics([record])

    assert report["unattended_merges"] == 1
    assert report["total_merges"] == 1


def test_hand_merged_auto_eligible_bead_is_not_counted_unattended():
    """A bead filed ``autonomy: auto`` is only an eligibility flag. If a
    person actually ran the merge (a User-typed merge actor in the record),
    that is attended — the outcome, not the filed flag, must win."""
    metrics = _load_metrics()
    record = _record(
        metrics,
        merged_by={"login": "grantbest", "type": "User"},
        autonomy="auto",
    )

    report = metrics.compute_metrics([record])

    assert report["unattended_merges"] == 0
    assert report["total_merges"] == 1


def test_outer_loop_pr_with_no_bead_reference_is_not_counted_unattended():
    metrics = _load_metrics()
    record = _record(
        metrics,
        body="Outer-loop: true\nRelease-gate: MERGE\n",
        merged_by={"login": "factory-bot", "type": "Bot"},
    )

    report = metrics.compute_metrics([record])

    assert report["unattended_merges"] == 0


def test_recognised_revert_trailer_counts_as_undone():
    metrics = _load_metrics()
    original = _record(metrics, pr_number=1, merge_commit="aaaaaaa1")
    revert = _record(
        metrics,
        pr_number=2,
        merge_commit="bbbbbbb2",
        merged_at="2026-08-02T00:00:00Z",
        body="This reverts commit aaaaaaa1.\n",
        merged_by={"login": "factory-bot", "type": "Bot"},
    )

    report = metrics.compute_metrics([original, revert])

    assert report["unattended_merges_undone"] == 1


def test_later_unrelated_fix_is_not_counted_as_undone():
    """A later PR that fixes something nearby, without a revert trailer
    naming the original merge commit, must not inflate the undone count —
    the acceptance criteria are explicit that a fix is not an undo."""
    metrics = _load_metrics()
    original = _record(metrics, pr_number=1, merge_commit="aaaaaaa1")
    later_fix = _record(
        metrics,
        pr_number=2,
        merge_commit="ccccccc3",
        merged_at="2026-08-02T00:00:00Z",
        body=f"Dispatched-bead id: {BEAD_ID}\nFixes the regression introduced by #1.\nRelease-gate: MERGE\n",
        merged_by={"login": "factory-bot", "type": "Bot"},
    )

    report = metrics.compute_metrics([original, later_fix])

    assert report["unattended_merges_undone"] == 0
    assert report["unattended_merges"] == 2


def test_time_to_undo_reports_unknown_not_zero_even_when_undone_exists():
    metrics = _load_metrics()
    original = _record(metrics, pr_number=1, merge_commit="aaaaaaa1")
    revert = _record(
        metrics,
        pr_number=2,
        merge_commit="bbbbbbb2",
        merged_at="2026-08-02T00:00:00Z",
        body="This reverts commit aaaaaaa1.\n",
        merged_by={"login": "factory-bot", "type": "Bot"},
    )

    report = metrics.compute_metrics([original, revert])

    assert report["time_to_undo"]["status"] == "unknown"
    assert report["time_to_undo"]["duration_seconds"] is None
    assert report["time_to_undo"]["reason"]


def test_window_is_carried_with_the_figures_and_defaults_to_record_span():
    metrics = _load_metrics()
    early = _record(metrics, pr_number=1, merge_commit="aaaaaaa1", merged_at="2026-08-01T00:00:00Z")
    late = _record(metrics, pr_number=2, merge_commit="bbbbbbb2", merged_at="2026-08-10T00:00:00Z")

    report = metrics.compute_metrics([early, late])

    assert report["window"] == {"since": "2026-08-01T00:00:00Z", "until": "2026-08-10T00:00:00Z"}


def test_explicit_window_filters_records_and_is_reported_verbatim():
    metrics = _load_metrics()
    inside = _record(metrics, pr_number=1, merge_commit="aaaaaaa1", merged_at="2026-08-05T00:00:00Z")
    outside = _record(metrics, pr_number=2, merge_commit="bbbbbbb2", merged_at="2026-09-01T00:00:00Z")

    report = metrics.compute_metrics(
        [inside, outside], since="2026-08-01T00:00:00Z", until="2026-08-31T00:00:00Z"
    )

    assert report["total_merges"] == 1
    assert report["window"] == {"since": "2026-08-01T00:00:00Z", "until": "2026-08-31T00:00:00Z"}


def test_compute_metrics_is_reproducible_given_the_same_records():
    metrics = _load_metrics()
    records = [
        _record(metrics, pr_number=1, merge_commit="aaaaaaa1"),
        _record(
            metrics,
            pr_number=2,
            merge_commit="bbbbbbb2",
            merged_by={"login": "grantbest", "type": "User"},
        ),
    ]

    first = json.dumps(metrics.compute_metrics(records), sort_keys=True)
    second = json.dumps(metrics.compute_metrics(records), sort_keys=True)

    assert first == second


def test_cli_reads_records_json_with_no_network_or_substrate(tmp_path, capsys):
    metrics = _load_metrics()
    records_path = tmp_path / "records.json"
    records_path.write_text(
        json.dumps(
            [
                {
                    "pr_number": 1,
                    "merge_commit": "aaaaaaa1",
                    "merged_at": "2026-08-01T00:00:00Z",
                    "body": f"Dispatched-bead id: {BEAD_ID}\nRelease-gate: MERGE\n",
                    "merged_by": {"login": "factory-bot", "type": "Bot"},
                    "autonomy": "auto",
                }
            ]
        )
    )

    code = metrics.main(["--records-json", str(records_path)])
    output = json.loads(capsys.readouterr().out)

    assert code == 0
    assert output["unattended_merges"] == 1
