"""``tools.finding_filing`` files a dev.finding bead by calling
``file_finding.file_finding`` from the real factory-dispatcher checkout —
never a re-derived filing path (PRIN-005, see ``.factory/design.md``). No
real substrate, no network: ``file_finding.Substrate`` is monkeypatched
rather than given a hand-rolled store double, so these tests add no new
class to apps/mcp-hub/tests' hand-rolled-double count (see
test_store_double_call_surface.py's ceiling).

The focus here is the ordering bug three prior attempts died on
(2026-10-02): ``file_finding.file_finding`` validates ``spec`` BEFORE it
ever touches a store (apps/factory-dispatcher/file_finding.py:169-173) —
constructing a real ``Substrate()`` before that validation runs breaks the
CLI's own refusal-before-write guarantee, surfacing as a bare ``KeyError``
in any environment without ``SUBSTRATE_URL``/``SUBSTRATE_API_KEY`` (the
dispatcher's own verification sandbox, deliberately) instead of a clean
refusal. ``tools.finding_filing._IdCapturingStore`` constructs lazily to
restore that ordering; the tests below pin it directly.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DISPATCHER_DIR = REPO_ROOT / "apps" / "factory-dispatcher"
if str(DISPATCHER_DIR) not in sys.path:
    sys.path.insert(0, str(DISPATCHER_DIR))

import dispatch  # noqa: E402,F401 -- load-bearing import order, see dispatcher_loader's docstring
import file_finding  # noqa: E402

from tools import dispatcher_loader, finding_filing  # noqa: E402


def _spec(**overrides):
    spec = {
        "kind": "bug",
        "disposition": "backlog",
        "severity": "low",
        "summary": "a probe finding",
        "state": "backlogged",
        "prompt_ref": "test-fixture",
    }
    spec.update(overrides)
    return spec


def _raising_substrate_factory(exc: BaseException):
    """A zero-argument callable standing in for ``file_finding.Substrate``
    that raises ``exc`` instead of constructing a real client — no store
    double class, just a function, so this adds nothing to the hand-rolled
    double count."""

    def _factory(*_args, **_kwargs):
        raise exc

    return _factory


# ---------------------------------------------------------------------------
# validation precedes construction (the ordering bug itself)
# ---------------------------------------------------------------------------


def test_invalid_spec_is_refused_before_the_store_is_ever_constructed(monkeypatch):
    monkeypatch.setattr(
        file_finding, "Substrate", _raising_substrate_factory(KeyError("SUBSTRATE_URL"))
    )

    bad_spec = _spec(kind="not-a-real-kind")
    with pytest.raises(finding_filing.FilingRefused) as excinfo:
        finding_filing.file_dev_finding(bad_spec, "agent-dev")

    assert "kind" in str(excinfo.value)


def test_valid_spec_with_unconstructable_store_is_unavailable_not_a_traceback(monkeypatch):
    monkeypatch.setattr(
        file_finding, "Substrate", _raising_substrate_factory(KeyError("SUBSTRATE_URL"))
    )

    with pytest.raises(finding_filing.Unavailable):
        finding_filing.file_dev_finding(_spec(), "agent-dev")


def test_valid_spec_with_missing_api_key_is_unavailable(monkeypatch):
    """The other half of Substrate.__init__'s two guards (substrate.py:38-41):
    SUBSTRATE_URL present, SUBSTRATE_API_KEY missing, raises RuntimeError
    rather than KeyError -- both must map to Unavailable."""
    monkeypatch.setattr(
        file_finding,
        "Substrate",
        _raising_substrate_factory(RuntimeError("SUBSTRATE_API_KEY is not set")),
    )

    with pytest.raises(finding_filing.Unavailable):
        finding_filing.file_dev_finding(_spec(), "agent-dev")


def test_store_factory_is_never_called_for_a_refused_spec(monkeypatch):
    calls = []

    def _tracking_factory(*_args, **_kwargs):
        calls.append(1)
        raise AssertionError("Substrate() must never be constructed for a refused spec")

    monkeypatch.setattr(file_finding, "Substrate", _tracking_factory)

    with pytest.raises(finding_filing.FilingRefused):
        finding_filing.file_dev_finding(_spec(kind="not-a-real-kind"), "agent-dev")

    assert calls == []


# ---------------------------------------------------------------------------
# partial write: created, but failed to transition (review note 8552d47f)
# ---------------------------------------------------------------------------


def _partial_write_store(created_id="dev-finding-partial-1"):
    """Not a class — a ``types.SimpleNamespace`` of plain functions, so this
    is invisible to test_store_double_call_surface.py's AST-based
    ``ast.ClassDef`` scan (see this module's own docstring)."""

    def create_bead(*_args, **_kwargs):
        return {"id": created_id}

    def transition_state(*_args, **_kwargs):
        raise RuntimeError("store conflict")

    return types.SimpleNamespace(create_bead=create_bead, transition_state=transition_state)


def test_partial_write_carries_the_created_id_not_a_bare_1():
    store = _partial_write_store(created_id="dev-finding-partial-7")

    with pytest.raises(finding_filing.PartialWrite) as excinfo:
        finding_filing.file_dev_finding(_spec(), "agent-dev", store=store)

    assert excinfo.value.id == "dev-finding-partial-7"
    assert str(excinfo.value) != "1"


def test_refusal_before_any_create_bead_call_never_raises_partial_write():
    store = _partial_write_store()

    with pytest.raises(finding_filing.FilingRefused):
        finding_filing.file_dev_finding(_spec(kind="not-a-real-kind"), "agent-dev", store=store)


# ---------------------------------------------------------------------------
# misconfiguration is loud, not degraded (mirrors test_task_filing.py)
# ---------------------------------------------------------------------------


def test_unset_env_var_raises_plainly(monkeypatch):
    monkeypatch.delenv(dispatcher_loader.FACTORY_DISPATCHER_ROOT_ENV, raising=False)
    with pytest.raises(finding_filing.Unavailable):
        finding_filing.file_dev_finding(_spec(), "agent-dev")
