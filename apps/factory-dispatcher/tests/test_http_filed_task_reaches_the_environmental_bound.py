"""AC-3 (M9, dropping the HTTP intake's filing-time pristine check, the Operator's
Option A, 2026-09-17): the refusal that used to fire at filing now fires at
claim time instead, so a bead whose declared verification can never pass
must not strand silently or re-dispatch forever. That mechanism already
exists and is covered generically by test_environmental_fault_bound.py; this
test proves it actually covers a bead filed the NEW way -- through
file_task.file_spec(run_pristine_verification=False), which is what
tools/task_filing.py calls for every HTTP filing -- ending in the same
"stop, not a verdict" bound-3 note observed working in production on bead
a728d995 (cited on this bead).

What this test does NOT do: re-implement or re-verify the bound-3 mechanism
itself (trailing_environmental_fault_streak, the note wording, the alert) --
that is test_environmental_fault_bound.py's job. This only closes the loop
from "filed via the new HTTP path" to "the existing stop fires."
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# The mcp-hub tree, so this test can drive the ACTUAL HTTP entry point rather
# than the function beneath it. tools.task_filing imports only stdlib
# (importlib, os, pathlib, sys, typing), so this pulls no fastapi/pydantic into
# the dispatcher's own CI job -- checked before adding it.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mcp-hub" / "src"))

import dispatch  # noqa: E402
import file_task  # noqa: E402
import retry_policy  # noqa: E402
from tools import task_filing  # noqa: E402


LIMIT = retry_policy.CONSECUTIVE_ENVIRONMENTAL_FAULT_LIMIT


class FakeSubstrate:
    def __init__(self):
        self._tasks: list[dict] = []
        self.notes_by_id: dict[str, list[dict]] = {}
        self.states: list[tuple] = []

    def list_tasks(self, state=None, limit=200):
        return list(self._tasks)

    def list_notes(self, parent_id, limit=500):
        return list(self.notes_by_id.get(parent_id) or [])

    def list_beads(self, namespace, type, **params):
        return []

    def list_links(self, bead_id, *, direction="both", link_type=None):
        return []

    def find_bead(self, namespace, type, content_ref):
        return None

    def create_task(self, content, created_by, *, trust_tier="user"):
        bead = {
            "id": "http-filed-1",
            "state": "pending",
            "created_at": "2026-09-17T00:00:00Z",
            "created_by": created_by,
            "content": content,
        }
        self._tasks.append(bead)
        return bead

    def add_note(self, parent_id, kind, body, created_by, provenance=None, **extra):
        note = {
            "id": f"note-{len(self.notes_by_id.get(parent_id, []))}",
            "created_at": f"2026-09-17T00:{len(self.notes_by_id.get(parent_id, [])):02d}:00Z",
            "content": {"kind": kind, "body": body, **extra},
        }
        self.notes_by_id.setdefault(parent_id, []).append(note)
        return note

    def set_state(self, task_id, state, created_by):
        self.states.append((task_id, state, created_by))


def _spec():
    return {
        "lane": "code-health",
        "title": "unrunnable verification, filed over HTTP",
        "intent": "reproduces a spec whose declared verification cannot pass",
        "acceptance": ["THE thing SHALL happen"],
        "scope": {"paths": ["apps/x/"]},
        "risk_class": "behavioral",
        "requirement_refs_waived": "test fixture; exercises unrelated behaviour",
        "release_ref_waived": "test fixture; exercises unrelated behaviour",
        # Shaped like a real verification command, but one that cannot pass
        # against an unmodified clone -- exactly OPS-6's shape. The HTTP path
        # must file this WITHOUT running it (that's AC-2's containment
        # property, covered in apps/mcp-hub/tests/test_task_filing.py); this
        # test picks up from "filed anyway" and drives it to claim time.
        "verification": {"commands": ["python -m pytest apps/x/nonexistent -q"]},
    }


def test_http_filed_task_with_unrunnable_verification_files_without_running_it():
    sub = FakeSubstrate()

    filed = file_task.file_spec(
        _spec(),
        "agent-dev",
        spec_identity=None,
        supersede=None,
        run_pristine_verification=False,
        sub=sub,
    )

    assert filed.bead["id"] == "http-filed-1"
    assert filed.bead["created_by"] == "agent-dev"
    # No environmental-fault note at filing time -- the refusal has not
    # happened yet, only been deferred to claim time.
    assert sub.list_notes("http-filed-1") == []


def test_http_filed_task_with_unrunnable_verification_reaches_the_bound(monkeypatch):
    """Simulates what activities.dispatch_steps.preflight_activity does at
    claim time when the declared verification fails against a real clone:
    it raises dispatch.DispatchEnvironmentError, and the dispatch workflow
    records that via dispatch.record_environment_failure. Three consecutive
    claims of the SAME unrunnable-verification bead must reach the bound-3
    stop -- not retry unbounded (starving every other pending bead, OPS-6's
    incident shape) and not burn the bead's retry budget (record_environment_
    failure never writes a "Run failed:" note; guards.prior_failures stays 0
    throughout, matching test_environmental_fault_bound.py's own assertion).
    """
    sub = FakeSubstrate()
    # file_dev_task locates the dispatcher tree through FACTORY_DISPATCHER_ROOT,
    # which the image sets (`ENV FACTORY_DISPATCHER_ROOT=/app`). Setting it here
    # means this test drives the same discovery the container does.
    monkeypatch.setenv("FACTORY_DISPATCHER_ROOT", str(Path(__file__).resolve().parents[3]))
    # CORRECTED 2026-09-17 (#909 gate, F2): this filed via file_task.file_spec
    # directly, so "filed over HTTP" was asserted by the test's name rather than
    # driven. It now goes through the real gateway entry point.
    bead = task_filing.file_dev_task(_spec(), "agent-dev", store=sub)
    reason = "same unrunnable verification command every time"

    for _ in range(LIMIT):
        dispatch.record_environment_failure(sub, bead, reason)

    notes = sub.list_notes(bead["id"])
    assert all(not n["content"]["body"].startswith(dispatch.RUN_FAILED_NOTE_PREFIX) for n in notes)
    latest = notes[-1]["content"]["body"]
    assert f"{LIMIT} consecutive times" in latest
    assert "this is a stop, not a verdict" in latest
    # AC-3 asks for the stop to be reachable WITH THE REASON READABLE ON THE
    # BOARD. Neither this test nor test_environmental_fault_bound.py asserted
    # that the reason survives into the note, so a stop that said only "the same
    # fault 3 consecutive times" would have passed both (#909 gate, F2).
    assert reason in latest, latest
    assert all(s == (bead["id"], "pending", dispatch.CREATED_BY) for s in sub.states)
