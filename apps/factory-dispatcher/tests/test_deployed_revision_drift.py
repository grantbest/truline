"""Schedule deployed_revision.describe_deployed_revision_drift -- the missing schedule
OPS-108's AC-5 deferral left open. No substrate, no network, no Temporal server, no live
cluster: land/announce are exercised against a fake store and a recording fake alert policy,
the same technique test_spec_record_reconcile.py already uses for this exact shape.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import deployed_revision  # noqa: E402
from activities import deployed_revision_drift as drd  # noqa: E402

NAMESPACE = "platform-mcp-prod"
DEPLOYMENT = "mcp-hub"


def drifted_status(*, deployed_sha="aaaaaaa", tracking_revision="ccccccc", pathspec_commits=2):
    return deployed_revision.DeployedRevisionStatus(
        namespace=NAMESPACE,
        deployment=DEPLOYMENT,
        deployed_sha=deployed_sha,
        tracking_ref="FETCH_HEAD",
        tracking_revision=tracking_revision,
        is_ancestor=True,
        pathspec_commits=pathspec_commits,
    )


def clear_status():
    return deployed_revision.DeployedRevisionStatus(
        namespace=NAMESPACE,
        deployment=DEPLOYMENT,
        deployed_sha="ccccccc",
        tracking_ref="FETCH_HEAD",
        tracking_revision="ccccccc",
        is_ancestor=True,
        pathspec_commits=0,
    )


def unreachable_status(error="kubectl exec failed: connection refused"):
    return deployed_revision.DeployedRevisionStatus.unreachable(
        namespace=NAMESPACE, deployment=DEPLOYMENT, error=error
    )


def sha_unknown_status():
    return deployed_revision.DeployedRevisionStatus.sha_unknown(
        namespace=NAMESPACE, deployment=DEPLOYMENT
    )


def fixed_now() -> datetime:
    return datetime(2026, 9, 12, 3, 0, tzinfo=timezone.utc)


def later() -> datetime:
    return datetime(2026, 9, 13, 3, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# configuration posture (PRIN-015) -- credentials and the kubectl target
# ---------------------------------------------------------------------------


def test_resolve_target_none_when_namespace_is_unset(monkeypatch):
    monkeypatch.delenv(drd.NAMESPACE_ENV, raising=False)
    monkeypatch.setenv(drd.DEPLOYMENT_ENV, "mcp-hub")
    assert drd.resolve_target() is None


def test_resolve_target_none_when_deployment_is_unset(monkeypatch):
    monkeypatch.setenv(drd.NAMESPACE_ENV, "platform-mcp-prod")
    monkeypatch.delenv(drd.DEPLOYMENT_ENV, raising=False)
    assert drd.resolve_target() is None


def test_resolve_target_returns_both_when_present(monkeypatch):
    monkeypatch.setenv(drd.NAMESPACE_ENV, "platform-mcp-prod")
    monkeypatch.setenv(drd.DEPLOYMENT_ENV, "mcp-hub")
    assert drd.resolve_target() == ("platform-mcp-prod", "mcp-hub")


def test_resolve_credentials_none_when_unset(monkeypatch):
    monkeypatch.delenv("SUBSTRATE_URL", raising=False)
    monkeypatch.delenv("SUBSTRATE_API_KEY", raising=False)
    assert drd.resolve_credentials() is None


# ---------------------------------------------------------------------------
# describe -- the env-configured target/remote, passed through explicitly
# ---------------------------------------------------------------------------


def test_describe_returns_none_when_not_configured(monkeypatch):
    monkeypatch.delenv(drd.NAMESPACE_ENV, raising=False)
    monkeypatch.delenv(drd.DEPLOYMENT_ENV, raising=False)
    assert drd.describe() is None


def test_describe_passes_remote_through_explicitly(monkeypatch):
    """FACTORY_REMOTE flows from the activity to the core comparison as an explicit
    argument, never re-read a second time inside deployed_revision.py."""
    monkeypatch.setenv(drd.NAMESPACE_ENV, NAMESPACE)
    monkeypatch.setenv(drd.DEPLOYMENT_ENV, DEPLOYMENT)

    captured = {}

    def fake_describe(**kwargs):
        captured.update(kwargs)
        return drifted_status()

    monkeypatch.setattr(
        drd.deployed_revision, "describe_deployed_revision_drift", fake_describe
    )

    status = drd.describe(remote="git@github.com:example/repo.git")

    assert status is not None
    assert captured["remote"] == "git@github.com:example/repo.git"
    assert captured["namespace"] == NAMESPACE
    assert captured["deployment"] == DEPLOYMENT


def test_describe_defaults_remote_to_factory_remote_env(monkeypatch):
    monkeypatch.setenv(drd.NAMESPACE_ENV, NAMESPACE)
    monkeypatch.setenv(drd.DEPLOYMENT_ENV, DEPLOYMENT)
    monkeypatch.setenv("FACTORY_REMOTE", "git@github.com:example/from-env.git")

    captured = {}

    def fake_describe(**kwargs):
        captured.update(kwargs)
        return clear_status()

    monkeypatch.setattr(
        drd.deployed_revision, "describe_deployed_revision_drift", fake_describe
    )

    drd.describe()

    assert captured["remote"] == "git@github.com:example/from-env.git"


# ---------------------------------------------------------------------------
# _condition_for -- the three-way split, never collapsing cannot-determine into either side
# ---------------------------------------------------------------------------


def test_condition_for_drifted():
    assert drd._condition_for(drifted_status()) == drd.DRIFTED


def test_condition_for_clear():
    assert drd._condition_for(clear_status()) == drd.CLEAR


def test_condition_for_unreachable():
    assert drd._condition_for(unreachable_status()) == drd.UNREACHABLE


def test_condition_for_sha_unknown():
    assert drd._condition_for(sha_unknown_status()) == drd.SHA_UNKNOWN


# ---------------------------------------------------------------------------
# land_deployed_revision_drift -- one standing observation, idempotent intake
# ---------------------------------------------------------------------------


class FakeDeployedRevisionDriftStore:
    def __init__(self):
        self.observations: dict[str, dict] = {}
        self._next_id = 1

    def find_observation(self, ref):
        return self.observations.get(ref)

    def create_observation(self, payload):
        assert payload["namespace"] == "arch"
        assert payload["type"] == "observation"
        assert payload["created_by"] == drd.CREATED_BY
        assert payload["content"]["source_class"] == "observed"  # #800 enrolled this identity
        ref = payload["content"]["ref"]
        assert ref not in self.observations, "create called for an existing ref"
        bead = {
            "id": f"obs-{self._next_id}",
            "created_by": payload["created_by"],
            "state": payload["state"],
            "content": payload["content"],
            "context": payload["context"],
        }
        self._next_id += 1
        self.observations[ref] = bead
        return dict(bead)

    def update_observation(self, bead_id, *, content=None, context=None, state=None):
        for bead in self.observations.values():
            if bead["id"] == bead_id:
                if content is not None:
                    bead["content"] = content
                if context is not None:
                    bead["context"] = context
                if state is not None:
                    bead["state"] = state
                return dict(bead)
        raise AssertionError(f"update_observation called for unknown bead_id {bead_id!r}")


def test_land_creates_one_standing_observation_for_drift():
    store = FakeDeployedRevisionDriftStore()
    result = drd.land_deployed_revision_drift(store, drifted_status(), now_fn=fixed_now)

    assert result["action"] == "created"
    assert result["condition"] == drd.DRIFTED
    bead = store.observations[drd.OBSERVATION_REF]
    assert bead["state"] == "active"
    assert bead["context"]["deployed_sha"] == "aaaaaaa"


def test_land_updates_the_same_bead_on_a_second_unchanged_run():
    store = FakeDeployedRevisionDriftStore()
    drd.land_deployed_revision_drift(store, drifted_status(), now_fn=fixed_now)
    result = drd.land_deployed_revision_drift(store, drifted_status(), now_fn=later)

    assert result["action"] == "updated"
    assert len(store.observations) == 1
    bead = store.observations[drd.OBSERVATION_REF]
    assert bead["context"]["first_observed_at"] == drd._iso(fixed_now())
    assert bead["context"]["last_observed_at"] == drd._iso(later())


def test_land_closes_the_observation_once_it_clears():
    store = FakeDeployedRevisionDriftStore()
    drd.land_deployed_revision_drift(store, drifted_status(), now_fn=fixed_now)
    result = drd.land_deployed_revision_drift(store, clear_status(), now_fn=later)

    assert result["action"] == "closed"
    assert store.observations[drd.OBSERVATION_REF]["state"] == "resolved"


def test_land_a_clean_run_with_no_prior_observation_writes_nothing():
    store = FakeDeployedRevisionDriftStore()
    result = drd.land_deployed_revision_drift(store, clear_status(), now_fn=fixed_now)

    assert result["action"] == "none"
    assert store.observations == {}


def test_land_reopens_a_resolved_observation_on_recurrence():
    store = FakeDeployedRevisionDriftStore()
    drd.land_deployed_revision_drift(store, drifted_status(), now_fn=fixed_now)
    drd.land_deployed_revision_drift(store, clear_status(), now_fn=later)
    result = drd.land_deployed_revision_drift(store, drifted_status(), now_fn=later)

    assert result["action"] == "updated"
    bead = store.observations[drd.OBSERVATION_REF]
    assert bead["state"] == "active"
    # a recurrence is a fresh episode -- first_observed_at resets, it does not inherit the
    # now-irrelevant start of the prior, already-resolved episode.
    assert bead["context"]["first_observed_at"] == drd._iso(later())


def test_deployed_revision_drift_land_sets_source_class_observed_and_enrolled_created_by_on_create():
    """#800 enrolled "factory-dispatcher/deployed-revision-drift" in bead_rules.py's
    SOURCE_CLASS_WRITERS["observed"] -- the fresh-ref (create) path must declare it."""
    store = FakeDeployedRevisionDriftStore()
    result = drd.land_deployed_revision_drift(store, drifted_status(), now_fn=fixed_now)

    assert result["action"] == "created"
    bead = store.observations[drd.OBSERVATION_REF]
    assert bead["content"]["source_class"] == "observed"
    assert bead["created_by"] == drd.CREATED_BY


def test_deployed_revision_drift_land_sets_source_class_observed_and_enrolled_created_by_on_update():
    """Same identity, existing-ref (update) path -- asserted by value against the stored
    bead since FakeDeployedRevisionDriftStore.update_observation accepts content without
    asserting on it."""
    store = FakeDeployedRevisionDriftStore()
    drd.land_deployed_revision_drift(store, drifted_status(deployed_sha="aaaaaaa"), now_fn=fixed_now)
    result = drd.land_deployed_revision_drift(
        store, drifted_status(deployed_sha="bbbbbbb"), now_fn=later
    )

    assert result["action"] == "updated"
    bead = store.observations[drd.OBSERVATION_REF]
    assert bead["content"]["source_class"] == "observed"
    assert bead["created_by"] == drd.CREATED_BY


def test_land_distinguishes_sha_unknown_from_drifted():
    store = FakeDeployedRevisionDriftStore()
    result = drd.land_deployed_revision_drift(store, sha_unknown_status(), now_fn=fixed_now)

    assert result["condition"] == drd.SHA_UNKNOWN
    bead = store.observations[drd.OBSERVATION_REF]
    assert bead["state"] == "active"  # still landed, never silent -- just not alerted


# ---------------------------------------------------------------------------
# alerting -- declared ALERT_INVENTORY entries, best-effort
# ---------------------------------------------------------------------------


class RecordingPolicy:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []

    async def send(self, kind, fingerprint, content, *, severity=None, min_interval_hours=0.0, members=None, **kwargs):
        self.sent.append((kind, {"fingerprint": fingerprint, "content": content, **kwargs}))
        return True


class ExplodingPolicy:
    async def send(self, *args, **kwargs):
        raise RuntimeError("webhook exploded")


def test_deployed_revision_drift_announce_drift_raises_the_declared_alert():
    policy = RecordingPolicy()
    posted = drd.announce_drift(drifted_status(), policy=policy)

    assert posted is True
    kind, payload = policy.sent[0]
    assert kind == f"deployed_revision_drift:{NAMESPACE}/{DEPLOYMENT}"
    assert "aaaaaaa" in payload["content"]
    assert payload["re_alert_interval_hours"] == drd.failure_diagnosis.ALERT_REALERT_INTERVAL_HOURS


def test_deployed_revision_drift_announce_drift_uses_the_declared_inventory_entry():
    definition = drd.notify.get_alert_definition(drd.DRIFT_ALERT_ID)
    assert not definition.is_removed
    assert definition.severity is not None
    assert definition.has_next_step


def test_deployed_revision_drift_announce_drift_is_best_effort_and_never_raises():
    assert drd.announce_drift(drifted_status(), policy=ExplodingPolicy()) is False


def test_deployed_revision_drift_announce_unreachable_raises_the_declared_alert():
    policy = RecordingPolicy()
    posted = drd.announce_unreachable(unreachable_status(), policy=policy)

    assert posted is True
    kind, payload = policy.sent[0]
    assert kind == f"deployed_revision_check_unreachable:{NAMESPACE}/{DEPLOYMENT}"
    assert "connection refused" in payload["content"]
    assert payload["re_alert_interval_hours"] == drd.failure_diagnosis.ALERT_REALERT_INTERVAL_HOURS


def test_deployed_revision_drift_announce_unreachable_uses_the_declared_inventory_entry():
    definition = drd.notify.get_alert_definition(drd.UNREACHABLE_ALERT_ID)
    assert not definition.is_removed
    assert definition.severity is not None
    assert definition.has_next_step


def test_deployed_revision_drift_announce_unreachable_is_best_effort_and_never_raises():
    assert drd.announce_unreachable(unreachable_status(), policy=ExplodingPolicy()) is False


def test_drift_fingerprint_excludes_tracking_revision():
    """An unchanged drift must dedup across nights even though main's tip (and therefore
    tracking_revision) moves on every merge (F6)."""
    first = drd._drift_fingerprint(drifted_status(tracking_revision="ccccccc"))
    second = drd._drift_fingerprint(drifted_status(tracking_revision="ddddddd"))
    assert first == second


def test_drift_fingerprint_changes_when_the_deployed_sha_changes():
    first = drd._drift_fingerprint(drifted_status(deployed_sha="aaaaaaa"))
    second = drd._drift_fingerprint(drifted_status(deployed_sha="bbbbbbb"))
    assert first != second


def test_deployed_revision_drift_repeated_identical_drift_is_suppressed_until_the_declared_interval(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_ALERT_STATE_PATH", str(tmp_path / "alert-state.json"))

    async def fake_post(_content, **_kwargs):
        fake_post.calls += 1
        return True

    fake_post.calls = 0
    policy = drd.notify.AlertPolicy(
        load_alert_state=drd.failure_diagnosis._load_dev_task_alert_state,
        record_alert_posted=drd.failure_diagnosis._record_dev_task_alert_posted,
        post=fake_post,
    )

    first = drd.announce_drift(drifted_status(tracking_revision="ccccccc"), policy=policy)
    # A later night, main has moved again (tracking_revision changed) but the pod is exactly
    # as drifted as before -- must still suppress, not repost.
    second = drd.announce_drift(drifted_status(tracking_revision="ddddddd"), policy=policy)

    assert first is True
    assert second is False
    assert fake_post.calls == 1


# ---------------------------------------------------------------------------
# the activity -- wiring, never reached by unit tests above this line
# ---------------------------------------------------------------------------


def test_activity_is_registered():
    names = {fn.__name__ for fn in drd.ACTIVITIES}
    assert "check_deployed_revision_drift_activity" in names


def test_activity_skips_when_not_configured(monkeypatch):
    monkeypatch.delenv(drd.NAMESPACE_ENV, raising=False)
    monkeypatch.delenv(drd.DEPLOYMENT_ENV, raising=False)

    result = drd.check_deployed_revision_drift_activity(None)

    assert result["status"] == "skipped"
    assert result["condition"] == "skipped_not_configured"


def test_activity_skips_when_credless(monkeypatch):
    monkeypatch.setenv(drd.NAMESPACE_ENV, NAMESPACE)
    monkeypatch.setenv(drd.DEPLOYMENT_ENV, DEPLOYMENT)
    monkeypatch.delenv("SUBSTRATE_URL", raising=False)
    monkeypatch.delenv("SUBSTRATE_API_KEY", raising=False)

    result = drd.check_deployed_revision_drift_activity(None)

    assert result["status"] == "skipped"
    assert result["condition"] == "skipped_credless"


# ---------------------------------------------------------------------------
# fresh-interpreter import -- this module must import cleanly first (F5)
# ---------------------------------------------------------------------------


def test_deployed_revision_drift_module_imports_cleanly_in_a_fresh_interpreter():
    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", "import activities.deployed_revision_drift"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
