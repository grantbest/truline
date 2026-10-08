"""A base ref can be WRONG (carries commits its tracking upstream never had)
without ever being STALE (behind it) -- and the pre-existing guard only ever
checked staleness.

2026-09-17: an operator repo's local `main` was pointed at a commit that
existed only on an unmerged pull request's branch (PR #882). `upstream_only`
(commits reachable from the tracking upstream but not the local ref) was 0 --
the tracking remote had nothing the local ref lacked -- so
`BaseRefStatus.behind_upstream` read False, and `dispatch.ensure_base_ref_current`'s
old healthy-path early return (`if not status.behind_upstream: return status`)
fired before `local_only` (commits reachable from the local ref but not its
upstream) was ever inspected for that branch. `schedule_status.py` made the
identical mistake independently: `BaseRefLiveStatus.stale` was defined as
`bool(self.commits_behind)`, discarding the `local_only` field it already
carried. Both reported healthy/not-stale throughout the incident.

Every test below is built from hand-constructed `BaseRefStatus`/
`BaseRefLiveStatus` fixtures -- no live clone, no network (CLAUDE.md's
2026-09-13 decision record D7) -- and from monkeypatching
`dispatch.base_ref_status` so `dispatch.ensure_base_ref_current`'s branching
logic can be exercised without a real git repository either.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

import dispatch  # noqa: E402
import schedule_status  # noqa: E402
from schedule_status import BaseRefLiveStatus  # noqa: E402


def _status(*, local_only: int, upstream_only: int) -> dispatch.BaseRefStatus:
    return dispatch.BaseRefStatus(
        base_ref="main",
        local_rev="a" * 40,
        upstream_ref="origin/main",
        upstream_rev="b" * 40,
        local_only=local_only,
        upstream_only=upstream_only,
    )


HEALTHY = _status(local_only=0, upstream_only=0)
STALE = _status(local_only=0, upstream_only=4)
WRONG = _status(local_only=3, upstream_only=0)
DIVERGED = _status(local_only=3, upstream_only=4)


# ---------------------------------------------------------------------------
# BaseRefStatus.classification / .wrong -- the four cases, at the dataclass
# level, independent of any rendering or refusal logic built on top of them.
# ---------------------------------------------------------------------------


def test_base_ref_status_classifies_all_four_cases_distinctly():
    assert HEALTHY.classification == "healthy"
    assert STALE.classification == "stale"
    assert WRONG.classification == "wrong"
    assert DIVERGED.classification == "diverged"

    assert HEALTHY.wrong is False
    assert STALE.wrong is False
    assert WRONG.wrong is True
    assert DIVERGED.wrong is True

    assert HEALTHY.behind_upstream is False
    assert STALE.behind_upstream is True
    assert WRONG.behind_upstream is False
    assert DIVERGED.behind_upstream is True


def test_base_ref_stale_error_message_distinguishes_wrong_from_stale():
    stale_message = str(dispatch.BaseRefStaleError(STALE))
    wrong_message = str(dispatch.BaseRefStaleError(WRONG))
    diverged_message = str(dispatch.BaseRefStaleError(DIVERGED))

    # The pure-stale message is byte-identical to what this replaces --
    # AC: "existing stale-base behaviour SHALL be preserved exactly".
    assert stale_message == (
        "base ref is behind its tracking remote; refused to clone: "
        + STALE.describe()
    )
    assert "WRONG" not in stale_message
    assert "refused to clone" in stale_message

    # Wrong and diverged both say WRONG, name the offending commit, and say
    # a fast-forward will not help -- the operator's remedy (repoint) differs
    # from the stale remedy (fetch/fast-forward), so the message must too.
    for message, status in ((wrong_message, WRONG), (diverged_message, DIVERGED)):
        assert "refused to clone" in message
        assert "WRONG" in message
        assert "fast-forward" in message and "will not fix it" in message
        assert status.local_rev in message
        assert status.upstream_ref in message
        assert status.upstream_rev in message
        assert f"{status.local_only}" in message


def test_source_mirror_base_ref_wrong_error_names_the_mirror_not_the_checkout():
    # dev.finding ac4e8569: the outer hop's refusal must (a) classify the same
    # way BaseRefStaleError's WRONG case does -- is_stale_base_ref_reason keys
    # off "refused to clone:" alone, by text, since the exception type does not
    # survive the Temporal activity boundary -- and (b) NOT reuse
    # BaseRefStaleError's wording, which names status.base_ref (the worker
    # checkout's own ref) as needing repointing. Here it is the MIRROR's main
    # that must be repointed, a different repository, so the message must say
    # so explicitly rather than send an operator to fix the wrong checkout.
    error = dispatch.SourceMirrorBaseRefWrongError(
        "main", "a" * 40, "origin/main", "b" * 40, 2
    )
    message = str(error)

    assert dispatch.is_stale_base_ref_reason(message)
    assert "WRONG" in message
    assert "fast-forward" in message and "will not fix it" in message
    assert "a" * 40 in message
    assert "origin/main" in message
    assert "b" * 40 in message
    assert "2" in message
    assert "SOURCE MIRROR" in message
    assert "worker checkout" in message


# ---------------------------------------------------------------------------
# dispatch.ensure_base_ref_current -- the actual gate. Fixture-only: no live
# git repo, `base_ref_status`/the two sync hops are monkeypatched away so the
# branching logic is exercised in isolation from the git plumbing that other
# tests (test_dispatch.py) already exercise for the stale-only case.
# ---------------------------------------------------------------------------


@pytest.fixture
def no_op_sync(monkeypatch):
    """Stub the two network/mirror hops `ensure_base_ref_current` runs first,
    so a fixture status can drive its branching without any real repo.

    The `_refresh_stale_tracking_ref` stub takes `force` because the real one
    does: the `status.wrong` branch calls it with `force=True` before accusing
    (gate finding 1 on #925). A stub that accepted less than the live contract
    is what turned that production change into two red tests here -- the
    mirror image of the usual trap, and the reason this signature is spelled
    out rather than swallowed by `**_k`. Keep it exact: if it drifts from the
    real signature again, these tests should fail loudly rather than pass on a
    double that cannot be called the way production calls it.
    """
    monkeypatch.setattr(dispatch, "_sync_source_mirror_main", lambda _cfg: None)
    monkeypatch.setattr(
        dispatch, "_refresh_stale_tracking_ref", lambda _cfg, *, force=False: None
    )
    monkeypatch.setattr(dispatch, "_other_clone_in_flight", lambda *_a, **_k: ())


def _cfg(tmp_path) -> dispatch.Config:
    return dispatch.Config(repo_root=tmp_path, base_ref="main")


def test_ensure_base_ref_current_returns_healthy_status_untouched(
    tmp_path, monkeypatch, no_op_sync
):
    monkeypatch.setattr(dispatch, "base_ref_status", lambda _cfg: HEALTHY)
    monkeypatch.setattr(
        dispatch,
        "_fast_forward_base_ref",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not fast-forward")),
    )
    alerts = []
    monkeypatch.setattr(
        dispatch.failure_diagnosis,
        "announce_base_ref_needs_person",
        lambda *a, **k: alerts.append((a, k)),
    )

    result = dispatch.ensure_base_ref_current(_cfg(tmp_path))

    assert result == HEALTHY
    assert alerts == []


def test_ensure_base_ref_current_refuses_when_wrong_but_not_behind(
    tmp_path, monkeypatch, no_op_sync
):
    # This is exactly the 2026-09-17 shape: upstream_only == 0 (not behind at
    # all), so the OLD `if not status.behind_upstream: return status` would
    # have returned healthy here without ever looking at local_only.
    monkeypatch.setattr(dispatch, "base_ref_status", lambda _cfg: WRONG)
    monkeypatch.setattr(
        dispatch,
        "_fast_forward_base_ref",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not fast-forward")),
    )
    alerts = []
    monkeypatch.setattr(
        dispatch.failure_diagnosis,
        "announce_base_ref_needs_person",
        lambda *a, **k: alerts.append((a, k)),
    )

    with pytest.raises(dispatch.BaseRefStaleError) as excinfo:
        dispatch.ensure_base_ref_current(_cfg(tmp_path))

    assert dispatch.is_stale_base_ref_reason(str(excinfo.value))
    assert "WRONG" in str(excinfo.value)

    assert len(alerts) == 1
    args, _kwargs = alerts[0]
    assert args[:4] == ("main", WRONG.local_rev, "origin/main", WRONG.upstream_rev)
    assert "local-only commit" in args[4]


def test_ensure_base_ref_current_re_measures_before_accusing_a_stale_tracking_ref(
    tmp_path, monkeypatch, no_op_sync
):
    """GATE FINDING 1 (#925): the false-positive fix itself must be pinned.

    Every other test here stubs `base_ref_status` to a CONSTANT, so none of
    them can observe the first-measurement-wrong / second-measurement-healthy
    sequence that the production fix exists to produce. With only those tests,
    deleting `force=True` -- or deleting the re-measure entirely -- fails
    nothing, and the defect silently returns.

    The shape is the real one: `worker_checkout.advance()` does a PATH fetch
    (`git fetch <source-path> main`), which writes FETCH_HEAD and never moves
    refs/remotes/origin/main, then `git branch -f main <tip>`. So immediately
    after a routine advance, local main is ahead of an unmoved tracking ref,
    `local_only > 0`, and the first measurement reads WRONG on a checkout that
    is perfectly correct. The staleness bound cannot self-correct it, because
    advance() has just written FETCH_HEAD.

    Asserted here: the forced refresh happens, it is forced (`force=True`, not
    the default), the re-measured healthy status is what comes back, nothing
    raises, and no urgent page advising a DESTRUCTIVE remedy is announced.

    The control -- a ref that is GENUINELY wrong stays wrong across the
    refetch and still refuses -- is
    test_ensure_base_ref_current_refuses_when_wrong_but_not_behind above,
    which stubs WRONG constantly and therefore returns WRONG from both
    measurements.
    """
    measurements = [WRONG, HEALTHY]
    monkeypatch.setattr(
        dispatch, "base_ref_status", lambda _cfg: measurements.pop(0)
    )

    refreshes = []
    monkeypatch.setattr(
        dispatch,
        "_refresh_stale_tracking_ref",
        lambda _cfg, *, force=False: refreshes.append(force),
    )
    monkeypatch.setattr(
        dispatch,
        "_fast_forward_base_ref",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not fast-forward")),
    )
    alerts = []
    monkeypatch.setattr(
        dispatch.failure_diagnosis,
        "announce_base_ref_needs_person",
        lambda *a, **k: alerts.append((a, k)),
    )

    result = dispatch.ensure_base_ref_current(_cfg(tmp_path))

    assert result == HEALTHY
    assert alerts == []
    # Both measurements were consumed: it did not short-circuit on the first.
    assert measurements == []
    # The unforced opening refresh, then the FORCED one on the wrong branch.
    assert refreshes == [False, True], refreshes


def test_ensure_base_ref_current_refuses_as_wrong_when_diverged_not_merely_stale(
    tmp_path, monkeypatch, no_op_sync
):
    # Diverged (both local_only and upstream_only > 0): AC says this must be
    # refused as WRONG, not merely reported/treated as stale, because
    # fast-forwarding does not fix it.
    monkeypatch.setattr(dispatch, "base_ref_status", lambda _cfg: DIVERGED)
    monkeypatch.setattr(
        dispatch,
        "_fast_forward_base_ref",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not fast-forward")),
    )
    alerts = []
    monkeypatch.setattr(
        dispatch.failure_diagnosis,
        "announce_base_ref_needs_person",
        lambda *a, **k: alerts.append((a, k)),
    )

    with pytest.raises(dispatch.BaseRefStaleError) as excinfo:
        dispatch.ensure_base_ref_current(_cfg(tmp_path))

    assert "WRONG" in str(excinfo.value)
    assert len(alerts) == 1


def test_ensure_base_ref_current_still_fast_forwards_when_merely_stale(
    tmp_path, monkeypatch, no_op_sync
):
    # AC: the existing stale-base behaviour (fast-forward attempted, no
    # alert) must be preserved exactly for the pure-stale case.
    monkeypatch.setattr(dispatch, "base_ref_status", lambda _cfg: STALE)
    calls = []
    monkeypatch.setattr(
        dispatch, "_fast_forward_base_ref", lambda cfg, status: calls.append(status)
    )
    alerts = []
    monkeypatch.setattr(
        dispatch.failure_diagnosis,
        "announce_base_ref_needs_person",
        lambda *a, **k: alerts.append((a, k)),
    )

    result = dispatch.ensure_base_ref_current(_cfg(tmp_path))

    assert calls == [STALE]
    assert alerts == []
    assert result == STALE


def test_ensure_base_ref_current_forwards_force_tracking_refresh_to_the_opening_refresh(
    tmp_path, monkeypatch, no_op_sync
):
    """AC-1 (dev.finding 5170b3f9a): force_tracking_refresh is a keyword, not a second
    implementation -- it must reach the OPENING call to _refresh_stale_tracking_ref as
    force=, with no other branching changed. The wrong-branch re-measure at :1530 keeps
    forcing on its own regardless of this flag -- that control is already pinned by
    test_ensure_base_ref_current_re_measures_before_accusing_a_stale_tracking_ref above.
    """
    monkeypatch.setattr(dispatch, "base_ref_status", lambda _cfg: HEALTHY)
    refreshes = []
    monkeypatch.setattr(
        dispatch,
        "_refresh_stale_tracking_ref",
        lambda _cfg, *, force=False: refreshes.append(force),
    )

    dispatch.ensure_base_ref_current(_cfg(tmp_path), force_tracking_refresh=True)
    assert refreshes == [True]

    refreshes.clear()
    dispatch.ensure_base_ref_current(_cfg(tmp_path))
    assert refreshes == [False]


def test_ensure_base_ref_current_does_not_report_healthy_when_unmeasurable(
    tmp_path, monkeypatch, no_op_sync
):
    # AC-3: where the authoritative ref cannot be reached (here: base_ref_status
    # itself cannot resolve it), the check must fail closed -- raise, never
    # silently return a healthy BaseRefStatus.
    def _boom(_cfg):
        raise dispatch.DispatchError("no upstream configured for main")

    monkeypatch.setattr(dispatch, "base_ref_status", _boom)

    with pytest.raises(dispatch.DispatchError):
        dispatch.ensure_base_ref_current(_cfg(tmp_path))


# ---------------------------------------------------------------------------
# schedule_status rendering -- the four cases must render differently, and
# the could-not-determine case must be distinguishable from all of them
# (never silently "healthy").
# ---------------------------------------------------------------------------


def _live(status: dispatch.BaseRefStatus) -> BaseRefLiveStatus:
    return BaseRefLiveStatus.from_base_ref_status(status)


def test_render_distinguishes_all_four_base_ref_classifications():
    rendered = {
        name: schedule_status.render_schedule_status(
            _quiet_status(), namespace="dev", base_ref_status=_live(status)
        )
        for name, status in (
            ("healthy", HEALTHY),
            ("stale", STALE),
            ("wrong", WRONG),
            ("diverged", DIVERGED),
        )
    }

    # All four renders are distinct strings.
    assert len(set(rendered.values())) == 4

    assert "base_ref_stale: false" in rendered["healthy"]
    assert "base_ref_wrong: false" in rendered["healthy"]
    assert "base_ref_state: healthy" in rendered["healthy"]
    assert "STALE BASE REF" not in rendered["healthy"]
    assert "WRONG BASE REF" not in rendered["healthy"]

    assert "base_ref_stale: true" in rendered["stale"]
    assert "base_ref_wrong: false" in rendered["stale"]
    assert "base_ref_state: stale" in rendered["stale"]
    assert "STALE BASE REF" in rendered["stale"]
    assert "WRONG BASE REF" not in rendered["stale"]

    assert "base_ref_wrong: true" in rendered["wrong"]
    assert "base_ref_state: wrong" in rendered["wrong"]
    assert "WRONG BASE REF" in rendered["wrong"]
    assert "STALE BASE REF" not in rendered["wrong"]

    # Diverged is reported as WRONG, not (also/instead) as merely stale.
    assert "base_ref_wrong: true" in rendered["diverged"]
    assert "base_ref_state: wrong" in rendered["diverged"]
    assert "WRONG BASE REF" in rendered["diverged"]
    assert "STALE BASE REF" not in rendered["diverged"]


def test_could_not_determine_is_distinguishable_from_healthy_and_wrong():
    status = BaseRefLiveStatus.could_not_determine("main", "no upstream configured")

    rendered = schedule_status.render_schedule_status(
        _quiet_status(), namespace="dev", base_ref_status=status
    )

    assert "base_ref_stale: could-not-determine" in rendered
    assert "base_ref_wrong: could-not-determine" in rendered
    assert "base_ref_stale: false" not in rendered
    assert "base_ref_stale: true" not in rendered
    assert "base_ref_wrong: false" not in rendered
    assert "base_ref_wrong: true" not in rendered
    assert "base_ref_state" not in rendered
    assert "STALE BASE REF" not in rendered
    assert "WRONG BASE REF" not in rendered


def test_wrong_base_ref_produces_one_urgent_notification():
    notifications = schedule_status.factory_health_notifications(
        _quiet_status(),
        interval_seconds=900,
        base_ref_status=_live(WRONG),
    )

    matching = [n for n in notifications if n.source == "base-ref-wrong"]
    assert len(matching) == 1
    assert matching[0].severity == "urgent"
    assert WRONG.local_rev in matching[0].detail
    assert WRONG.upstream_rev in matching[0].detail
    assert [n for n in notifications if n.source == "base-ref-stale"] == []


def test_diverged_base_ref_notifies_as_wrong_not_as_stale():
    notifications = schedule_status.factory_health_notifications(
        _quiet_status(),
        interval_seconds=900,
        base_ref_status=_live(DIVERGED),
    )

    assert [n for n in notifications if n.source == "base-ref-wrong"] != []
    assert [n for n in notifications if n.source == "base-ref-stale"] == []


def _quiet_status():
    from schedule_runtime import FactoryScheduleStatus

    return FactoryScheduleStatus(
        schedule_id="factory-dispatcher-dev",
        paused=False,
        in_flight=(),
        recent=(),
    )
