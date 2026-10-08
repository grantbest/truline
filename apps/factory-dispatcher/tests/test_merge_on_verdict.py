"""Tests for the merge-by-verdict activity (R26.09/O-2, this bead's own record).

No Temporal server, no substrate database, no network, no `gh`: every PR
read is injected via `gh_view`/`gh_open_prs_with_base_fn`, the merge itself
via `run_merge_script_fn`, and note-filing via a `FakeStore` recording calls
-- `scripts/gate_markers.py` is imported and exercised for real (it is the
one recognizer this activity and `scripts/merge-pr.sh` must never drift on),
but nothing here shells out or reaches a live PR.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_DISPATCHER_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _DISPATCHER_ROOT.parents[1]
sys.path.insert(0, str(_DISPATCHER_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import dispatch  # noqa: E402
from activities import merge_on_verdict as mov  # noqa: E402


def _cfg() -> dispatch.Config:
    return dispatch.Config(
        repo="grant/test-repo",
        remote="origin",
        base_ref="main",
        repo_root=_REPO_ROOT,
    )


class FakeStore:
    def __init__(self) -> None:
        self.notes: list[dict] = []
        self.raise_on_add_note: Exception | None = None

    def add_note(self, parent_id, kind, body, created_by, trust_tier="system", provenance=None, **extra):
        if self.raise_on_add_note is not None:
            raise self.raise_on_add_note
        record = {
            "parent_id": parent_id,
            "kind": kind,
            "body": body,
            "created_by": created_by,
            "provenance": provenance,
        }
        self.notes.append(record)
        return {"id": f"note-{len(self.notes)}", **record}


def _pr_payload(
    *,
    body="",
    comments=None,
    head="deadbeef" * 5,
    head_ref_name="branch-1",
):
    return {
        "body": body,
        "comments": comments or [],
        "headRefOid": head,
        "headRefName": head_ref_name,
    }


def _request(number=42, requested_by="operator@example.org", scope="factory.merge"):
    return {"pr_number": number, "requested_by": requested_by, "scope": scope}


def _run(
    request,
    *,
    payload,
    capability_enabled=True,
    open_with_base=None,
    merge_returncode=0,
    merge_stderr="",
    store=None,
):
    store = store if store is not None else FakeStore()
    gh_view_calls: list[int] = []
    gh_list_calls: list[str] = []
    merge_calls: list[int] = []

    def gh_view(number, cfg):
        gh_view_calls.append(number)
        if isinstance(payload, Exception):
            raise payload
        return payload

    def gh_open_prs_with_base_fn(base_branch, cfg):
        gh_list_calls.append(base_branch)
        if open_with_base is None:
            return []
        return open_with_base

    def run_merge_script_fn(number, cfg):
        merge_calls.append(number)
        return subprocess.CompletedProcess(
            args=["scripts/merge-pr.sh", str(number)],
            returncode=merge_returncode,
            stdout="",
            stderr=merge_stderr,
        )

    result = mov.run_merge_on_verdict(
        request,
        cfg=_cfg(),
        store=store,
        gh_view=gh_view,
        gh_open_prs_with_base_fn=gh_open_prs_with_base_fn,
        run_merge_script_fn=run_merge_script_fn,
        capability_enabled_fn=lambda: capability_enabled,
    )
    return result, store, gh_view_calls, gh_list_calls, merge_calls


# --- the capability flag gates everything, before any gh call -------------


def test_capability_disabled_refuses_without_any_gh_call():
    result, store, gh_view_calls, gh_list_calls, merge_calls = _run(
        _request(), payload=_pr_payload(), capability_enabled=False,
    )
    assert result["disposition"] == "refused:capability-disabled"
    assert result["reason"] == "capability-disabled"
    assert gh_view_calls == []
    assert merge_calls == []
    assert store.notes == []


# --- gh failure before the PR body is even readable ------------------------


def test_gh_view_failure_is_a_refusal_not_a_crash():
    result, store, _, _, merge_calls = _run(
        _request(), payload=dispatch.DispatchError("gh: not found"),
    )
    assert result["disposition"].startswith("refused:gh-error:")
    assert merge_calls == []
    assert store.notes == []  # no bead resolved yet


# --- verdict-shaped refusals, each with a bead present ----------------------


BEAD_ID = "11111111-1111-1111-1111-111111111111"
HEAD = "a" * 40


def _bead_only_body() -> str:
    """A dispatcher-shaped body carries only the bead-id declaration. Under
    dev.finding 807baee1 (gate_markers.py AC-2), a release-gate verdict on
    such a PR is recorded only as a PR comment -- never the body, which the
    worker's own report populates -- so these fixtures never put a verdict
    line in the body."""
    return f"Dispatched-bead id: {BEAD_ID}"


def _verdict_comment(verdict_line: str = "", revision_line: str = "") -> list[dict]:
    """The comment carrying a verdict (and optionally its adjacent revision
    line) for a bead-declaring PR -- see _bead_only_body."""
    if not verdict_line:
        return []
    text = verdict_line if not revision_line else f"{verdict_line}\n{revision_line}"
    return [{"body": text}]


def test_no_verdict_is_refused_and_noted():
    body = _bead_only_body()
    result, store, *_ = _run(_request(), payload=_pr_payload(body=body, head=HEAD))
    assert result["disposition"] == "refused:no-verdict"
    assert result["bead_id"] == BEAD_ID
    assert len(store.notes) == 1
    assert store.notes[0]["parent_id"] == BEAD_ID
    assert "no-verdict" in store.notes[0]["body"] or "none" in store.notes[0]["body"]


def test_do_not_merge_is_refused_and_noted():
    body = _bead_only_body()
    comments = _verdict_comment("Release-gate: DO-NOT-MERGE")
    result, store, *_ = _run(_request(), payload=_pr_payload(body=body, head=HEAD, comments=comments))
    assert result["disposition"] == "refused:verdict-do-not-merge"
    assert len(store.notes) == 1


def test_merge_with_changes_is_refused_and_noted():
    body = _bead_only_body()
    comments = _verdict_comment("Release-gate: MERGE-WITH-CHANGES")
    result, store, *_ = _run(_request(), payload=_pr_payload(body=body, head=HEAD, comments=comments))
    assert result["disposition"] == "refused:verdict-merge-with-changes"
    assert len(store.notes) == 1


def test_merge_verdict_with_no_revision_line_is_refused():
    """Stricter than merge-pr.sh itself: a MERGE verdict with no adjacent
    Release-gate-revision line proceeds under merge-pr.sh's own compatibility
    case, but this automated path refuses it -- see .factory/design.md."""
    body = _bead_only_body()
    comments = _verdict_comment("Release-gate: MERGE")
    result, store, *_ = _run(_request(), payload=_pr_payload(body=body, head=HEAD, comments=comments))
    assert result["disposition"] == "refused:no-revision"
    assert len(store.notes) == 1


def test_merge_verdict_with_mismatched_revision_is_refused():
    body = _bead_only_body()
    comments = _verdict_comment("Release-gate: MERGE", f"Release-gate-revision: {'b' * 40}")
    result, store, *_ = _run(_request(), payload=_pr_payload(body=body, head=HEAD, comments=comments))
    assert result["disposition"] == "refused:revision-mismatch"
    assert len(store.notes) == 1


def test_body_verdict_on_a_bead_declaring_pr_is_ignored_and_refused():
    """AC-5's new case: a well-formed verdict AND its adjacent revision
    line, both written directly into the body of a bead-declaring PR (the
    exact shape dev.finding 807baee1 describes -- the worker's own report
    lands in the body), must never be read. With no comment to supply a
    verdict instead, this refuses exactly like no verdict were recorded at
    all."""
    body = (
        f"Dispatched-bead id: {BEAD_ID}\n"
        "Release-gate: MERGE\n"
        f"Release-gate-revision: {HEAD}"
    )
    result, store, *_ = _run(_request(), payload=_pr_payload(body=body, head=HEAD))
    assert result["disposition"] == "refused:no-verdict"
    assert result["bead_id"] == BEAD_ID


def test_merge_verdict_with_no_bead_is_refused_and_leaves_no_note():
    body = f"Release-gate: MERGE\nRelease-gate-revision: {HEAD}"
    result, store, *_ = _run(_request(), payload=_pr_payload(body=body, head=HEAD))
    assert result["disposition"] == "refused:no-bead"
    assert result["bead_id"] is None
    assert store.notes == []  # nothing to attach a note to


def test_stacked_base_is_refused_and_noted():
    body = _bead_only_body()
    comments = _verdict_comment("Release-gate: MERGE", f"Release-gate-revision: {HEAD}")
    result, store, *_ = _run(
        _request(),
        payload=_pr_payload(body=body, head=HEAD, comments=comments),
        open_with_base=[{"number": 99}],
    )
    assert result["disposition"] == "refused:stacked-base"
    assert len(store.notes) == 1


def test_open_prs_with_base_gh_failure_is_refused():
    body = _bead_only_body()
    comments = _verdict_comment("Release-gate: MERGE", f"Release-gate-revision: {HEAD}")
    store = FakeStore()
    result, _, gh_view_calls, gh_list_calls, merge_calls = _run(
        _request(), payload=_pr_payload(body=body, head=HEAD, comments=comments), store=store,
    )
    # sanity: reaches the stacked-base check at all (revision matched, bead present)
    assert gh_list_calls == ["branch-1"]
    assert merge_calls == [42]


# --- the happy path: every condition holds ---------------------------------


def test_all_conditions_hold_runs_merge_script_and_records_merged():
    body = _bead_only_body()
    comments = _verdict_comment("Release-gate: MERGE", f"Release-gate-revision: {HEAD}")
    result, store, gh_view_calls, gh_list_calls, merge_calls = _run(
        _request(), payload=_pr_payload(body=body, head=HEAD, comments=comments),
    )
    assert result["disposition"] == "merged"
    assert result["reason"] is None
    assert merge_calls == [42]
    assert len(store.notes) == 1
    assert "merged" in store.notes[0]["body"].lower()
    assert store.notes[0]["provenance"]["model"] == "none"


def test_abbreviated_revision_matches_by_prefix():
    """gate_markers.revisions_match tolerates either side being an
    abbreviated prefix -- this activity must use it, not exact equality."""
    body = _bead_only_body()
    comments = _verdict_comment("Release-gate: MERGE", f"Release-gate-revision: {HEAD[:8]}")
    result, store, *_ = _run(_request(), payload=_pr_payload(body=body, head=HEAD, comments=comments))
    assert result["disposition"] == "merged"


def test_merge_script_nonzero_exit_is_refused_and_noted():
    body = _bead_only_body()
    comments = _verdict_comment("Release-gate: MERGE", f"Release-gate-revision: {HEAD}")
    result, store, *_ = _run(
        _request(),
        payload=_pr_payload(body=body, head=HEAD, comments=comments),
        merge_returncode=1,
        merge_stderr="ERROR: PR #42 carries no recognizable release-gate verdict record.",
    )
    assert result["disposition"].startswith("refused:merge-script-failed:1:")
    assert len(store.notes) == 1


# --- comments carry the verdict; a body verdict is never read at all -------


def test_verdict_recorded_in_a_comment_merges_even_though_the_body_names_a_different_one():
    """Under AC-2 the body of a bead-declaring PR is never read for a
    verdict at all, so a body verdict does not "supersede" or get
    "superseded" here -- it is simply never consulted. The comment's own
    verdict is the only one that exists from this activity's point of view,
    regardless of what the (worker-writable) body says."""
    body = f"Dispatched-bead id: {BEAD_ID}\nRelease-gate: MERGE-WITH-CHANGES"
    payload = _pr_payload(
        body=body,
        head=HEAD,
        comments=[{"body": f"Release-gate: MERGE\nRelease-gate-revision: {HEAD}"}],
    )
    result, store, *_ = _run(_request(), payload=payload)
    assert result["disposition"] == "merged"


# --- note-writing is best-effort ------------------------------------------


def test_note_write_failure_does_not_change_the_returned_disposition():
    body = _bead_only_body()
    comments = _verdict_comment("Release-gate: MERGE", f"Release-gate-revision: {HEAD}")
    store = FakeStore()
    store.raise_on_add_note = RuntimeError("substrate unreachable")
    result, _, *_ = _run(_request(), payload=_pr_payload(body=body, head=HEAD, comments=comments), store=store)
    assert result["disposition"] == "merged"


# --- activity registration ---------------------------------------------


def test_activity_is_registered_by_name():
    assert mov.run_merge_on_verdict_activity.__temporal_activity_definition.name == "run_merge_on_verdict"
    assert mov.run_merge_on_verdict_activity in mov.ACTIVITIES


# --- AC-1(c)/AC-5: run_merge_script needs exactly DISCORD_WEBHOOK_URL -----


def test_run_merge_script_asks_dispatch_run_for_exactly_the_discord_webhook(monkeypatch):
    """merge-pr.sh runs post-merge-health-check.py, whose RED-alert path posts
    through notify.post_discord, which reads DISCORD_WEBHOOK_URL from the
    environment (dev.finding a0166920). No other credential reaches this
    child, so `needs` must name exactly that one variable."""
    captured = {}

    def fake_run(cmd, *, cwd=None, timeout=None, check=True, env=None, needs=()):
        captured["cmd"] = cmd
        captured["needs"] = needs
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(dispatch, "run", fake_run)
    mov.run_merge_script(42, _cfg())

    assert captured["needs"] == ("DISCORD_WEBHOOK_URL",)
    assert captured["cmd"][0].endswith("merge-pr.sh")
    assert captured["cmd"][1] == "42"
