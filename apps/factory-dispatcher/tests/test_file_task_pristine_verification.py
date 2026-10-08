"""OPS-6, 2026-08-23: dev.task 05262e55's declared verification could never
pass — apps/substrate/src/database.py raised at import for want of
environment variables the spec never set — and nothing checked that at filing.
The dispatcher's own preflight step already runs exactly this check
(activities.dispatch_steps.preflight_activity calls
dispatch.verify_declared_commands); it just runs three hours and twelve clone
bootstraps too late. Filing now runs the same check, through the same
function, before a bead is ever created.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import dispatch  # noqa: E402
import file_task  # noqa: E402


def _spec(**overrides):
    spec = {
        "lane": "drift",
        "title": "t",
        "intent": "i",
        "acceptance": ["THE thing SHALL happen"],
        "scope": {"paths": ["apps/x/"]},
        "risk_class": "behavioral",
        "requirement_refs_waived": "test fixture; exercises unrelated behaviour",
        "release_ref_waived": "test fixture; exercises unrelated behaviour",
        "verification": {"commands": ["python -m pytest apps/x -q"]},
    }
    spec.update(overrides)
    return spec


class FakeSubstrate:
    def __init__(self, tasks=None):
        self._tasks = tasks or []
        self.posted = None
        self.notes = []

    def list_tasks(self, state=None, limit=200):
        return self._tasks

    def list_notes(self, parent_id, limit=500):
        return []

    def add_note(self, parent_id, kind, body, created_by, **extra):
        note = {"parent_id": parent_id, "kind": kind, "body": body}
        self.notes.append(note)
        return note

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


def _passed(command: str) -> dispatch.VerificationCommandResult:
    return dispatch.VerificationCommandResult(
        command=command, outcome="passed", exit_code=0, output="", duration_s=0.1
    )


def _failed(command: str, output: str = "ImportError: no module named foo") -> dispatch.VerificationCommandResult:
    return dispatch.VerificationCommandResult(
        command=command, outcome="failed", exit_code=1, output=output, duration_s=0.1
    )


def _file(tmp_path, **overrides):
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_spec(**overrides)))
    return spec_path


# ---------------------------------------------------------------------------
# the refusal
# ---------------------------------------------------------------------------


def test_failing_pristine_verification_refuses_filing_and_creates_no_bead(
    tmp_path, monkeypatch
):
    fake = FakeSubstrate()
    monkeypatch.setattr(file_task, "Substrate", lambda: fake)
    monkeypatch.setattr(
        dispatch,
        "verify_pristine_commands",
        lambda commands, cfg=None: dispatch.VerificationReport(
            (_failed("python -m pytest apps/x -q"),)
        ),
    )
    spec_path = _file(tmp_path)

    with pytest.raises(SystemExit) as exc:
        file_task.main([str(spec_path)])

    message = str(exc.value)
    assert "python -m pytest apps/x -q" in message
    assert "ImportError: no module named foo" in message
    assert fake.posted is None


def test_passing_pristine_verification_files_normally(tmp_path, monkeypatch):
    fake = FakeSubstrate()
    monkeypatch.setattr(file_task, "Substrate", lambda: fake)
    monkeypatch.setattr(
        dispatch,
        "verify_pristine_commands",
        lambda commands, cfg=None: dispatch.VerificationReport(
            (_passed("python -m pytest apps/x -q"),)
        ),
    )
    spec_path = _file(tmp_path)

    rc = file_task.main([str(spec_path)])

    assert rc == 0
    assert fake.posted is not None
    assert "pristine_verification_waived" not in fake.posted["content"]


def test_no_declared_verification_commands_skips_the_check(tmp_path, monkeypatch):
    fake = FakeSubstrate()
    monkeypatch.setattr(file_task, "Substrate", lambda: fake)

    def _boom(*_args, **_kwargs):
        raise AssertionError("verify_pristine_commands should not run with no commands")

    monkeypatch.setattr(dispatch, "verify_pristine_commands", _boom)
    spec_path = _file(tmp_path, verification={"commands": []})

    rc = file_task.main([str(spec_path)])

    assert rc == 0
    assert fake.posted is not None


# ---------------------------------------------------------------------------
# the escape hatch is gone
# ---------------------------------------------------------------------------
#
# OPS-7's --waive-pristine-verification recorded a reason and let filing
# through unconditionally, but the dispatcher's preflight never read that
# reason: filing accepted a bead that dispatch could then never run
# (2026-08-23). The flag no longer exists — its one legitimate use, a
# red-first command, is covered below by verification.expect_pristine_failure,
# which the dispatcher's own preflight (dispatch.expects_pristine_failure)
# actually honours.


def test_waive_pristine_verification_flag_no_longer_exists(tmp_path, monkeypatch):
    fake = FakeSubstrate()
    monkeypatch.setattr(file_task, "Substrate", lambda: fake)
    spec_path = _file(tmp_path)

    with pytest.raises(SystemExit) as exc:
        file_task.main([str(spec_path), "--waive-pristine-verification", "reason"])

    assert fake.posted is None
    assert exc.value.code != 0


# ---------------------------------------------------------------------------
# expect_pristine_failure — the real red-first mechanism
# ---------------------------------------------------------------------------


def test_expect_pristine_failure_files_normally_when_pristine_fails(tmp_path, monkeypatch):
    """A red-first task's declared verification is SUPPOSED to fail on main.

    Filing must not refuse it — that would make expect_pristine_failure
    unfileable for the one case it exists for.
    """
    fake = FakeSubstrate()
    monkeypatch.setattr(file_task, "Substrate", lambda: fake)
    monkeypatch.setattr(
        dispatch,
        "verify_pristine_commands",
        lambda commands, cfg=None: dispatch.VerificationReport(
            (_failed("python -m pytest apps/x -q"),)
        ),
    )
    spec_path = _file(
        tmp_path,
        verification={
            "commands": ["python -m pytest apps/x -q"],
            "expect_pristine_failure": True,
        },
    )

    rc = file_task.main([str(spec_path)])

    assert rc == 0
    assert fake.posted is not None
    assert fake.posted["content"]["verification"]["expect_pristine_failure"] is True


def test_expect_pristine_failure_refuses_filing_when_pristine_already_passes(
    tmp_path, monkeypatch
):
    """The bead's own red-first check already passing means the work is done.

    Filing this would mint a bead the dispatcher's preflight would then
    refuse as already-satisfied — refuse at filing instead, and name which
    command unexpectedly passed.
    """
    fake = FakeSubstrate()
    monkeypatch.setattr(file_task, "Substrate", lambda: fake)
    monkeypatch.setattr(
        dispatch,
        "verify_pristine_commands",
        lambda commands, cfg=None: dispatch.VerificationReport(
            (_passed("python -m pytest apps/x -q"),)
        ),
    )
    spec_path = _file(
        tmp_path,
        verification={
            "commands": ["python -m pytest apps/x -q"],
            "expect_pristine_failure": True,
        },
    )

    with pytest.raises(SystemExit) as exc:
        file_task.main([str(spec_path)])

    message = str(exc.value)
    assert "python -m pytest apps/x -q" in message
    assert "already" in message.lower()
    assert fake.posted is None


def test_expect_pristine_failure_still_refuses_on_startup_failure(tmp_path, monkeypatch):
    """A broken checker binary is not the declared red state — stays environmental."""
    fake = FakeSubstrate()
    monkeypatch.setattr(file_task, "Substrate", lambda: fake)
    monkeypatch.setattr(
        dispatch,
        "verify_pristine_commands",
        lambda commands, cfg=None: dispatch.VerificationReport(
            (
                dispatch.VerificationCommandResult(
                    command="missing-checker --strict",
                    outcome="could_not_start",
                    exit_code=127,
                    output="command not found",
                    duration_s=0.1,
                    reason="command not found",
                ),
            )
        ),
    )
    spec_path = _file(
        tmp_path,
        verification={
            "commands": ["missing-checker --strict"],
            "expect_pristine_failure": True,
        },
    )

    with pytest.raises(SystemExit) as exc:
        file_task.main([str(spec_path)])

    assert "could not even start" in str(exc.value)
    assert fake.posted is None


def test_no_expectation_and_failing_pristine_still_refuses_filing(tmp_path, monkeypatch):
    """OPS-6 behaviour must not regress: no expectation declared, pristine
    cannot pass -> refused at filing, same as before expect_pristine_failure
    existed."""
    fake = FakeSubstrate()
    monkeypatch.setattr(file_task, "Substrate", lambda: fake)
    monkeypatch.setattr(
        dispatch,
        "verify_pristine_commands",
        lambda commands, cfg=None: dispatch.VerificationReport(
            (_failed("python -m pytest apps/x -q"),)
        ),
    )
    spec_path = _file(tmp_path)

    with pytest.raises(SystemExit) as exc:
        file_task.main([str(spec_path)])

    assert "python -m pytest apps/x -q" in str(exc.value)
    assert fake.posted is None


# ---------------------------------------------------------------------------
# run_pristine_verification has no default (F4, #909 gate) — every caller of
# file_spec must state its intent; omitting it must not silently pick either
# behaviour.
# ---------------------------------------------------------------------------


def test_file_spec_requires_run_pristine_verification_to_be_stated():
    """A call to file_spec that omits run_pristine_verification must fail at
    the call site with TypeError, not fall back to some default -- this is
    what removing the default actually buys: a caller cannot forget."""
    with pytest.raises(TypeError):
        file_task.file_spec(_spec(), "test-caller")


# ---------------------------------------------------------------------------
# reuse, not reimplementation — the shared entry point
# ---------------------------------------------------------------------------


def test_filing_reuses_the_dispatchers_verify_declared_commands(tmp_path, monkeypatch):
    """file_task's pre-filing check must run declared verification through
    dispatch.verify_declared_commands — the EXACT function
    activities.dispatch_steps.preflight_activity calls on every dispatch, not
    a second copy of it. Patching the attribute on the dispatch module (not a
    reference file_task captured at import time) is what proves the two paths
    share one implementation: both file_task and activities.dispatch_steps
    only ever hold a reference to the dispatch MODULE, and look the function
    up on it at call time.
    """
    from activities import dispatch_steps

    calls = []

    def spy(clone, commands):
        calls.append((clone, tuple(commands)))
        return dispatch.VerificationReport((_passed(commands[0]),))

    monkeypatch.setattr(dispatch, "verify_declared_commands", spy)
    monkeypatch.setattr(dispatch, "make_clone", lambda cfg, dest: dest.mkdir(parents=True))

    fake = FakeSubstrate()
    monkeypatch.setattr(file_task, "Substrate", lambda: fake)
    spec_path = _file(tmp_path)

    rc = file_task.main([str(spec_path)])

    assert rc == 0
    assert calls and calls[0][1] == ("python -m pytest apps/x -q",)
    # the identical function object is what preflight_activity would call too
    assert dispatch_steps.dispatch.verify_declared_commands is spy
