#!/usr/bin/env python3
"""The console operator surface, proven by a dated probe (R26.09/O-7).

Reads what the console's own two gateway routes claim -- ``GET
/api/v1/factory/schedule_status`` (the factory-state panel's schedule half)
and ``POST /api/v1/factory/release_delivery`` (the release view), never by
importing the console's own modules and never through the substrate proxy --
then reads the SAME facts independently from their authorities: Temporal
directly for the schedule, and the shared substrate client's ``delivers``
edges and dev.task states for release delivery. ``run_probe`` diffs the two
readings field by field, per group, and ``record_observation`` lands one
standing ``arch.observation`` with the verdict.

WHAT IS NOT PROBED (see ``NOT_PROBED`` below): the factory-state panel's
queue half and worker half, both read by the console over the substrate
proxy that no machine token may take; the release index's browser-computed
outcome counts; any rendering; and the Cloudflare Access / ForwardAuth hop
in front of the gateway -- this module calls its own two routes over
loopback with a self-minted scope header, which proves the route handlers
and the data behind them, not the front door.

Runnable two ways from one implementation: ``main()`` below is the CLI;
``apps/mcp-hub/src/tools/factory_probe.py`` imports this module (the way
``tools/task_filing.py`` imports ``file_task``) and calls the same
``run_and_record`` this CLI calls, from ``GET /api/v1/factory/probe``.

Read-only except for the one standing observation this module writes: there
is no target bead, no note, no schedule mutation -- the phone-path probe's
target-bead mechanism (PROBE-1) is retired, not reused.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import process_env

#: The fixed writer identity this bead enrolls under
#: ``apps/substrate/src/bead_rules.py``'s ``SOURCE_CLASS_WRITERS["observed"]``
#: -- exact-identity match, required before any write here can land (409
#: otherwise).
CREATED_BY = "factory-dispatcher/probe-console-surface"

#: content.ref of the one standing bead this module ever writes -- found and
#: updated in place, never minted per run.
OBSERVATION_REF = "obs.probe.console-surface"

OBSERVATION_SOURCE_CLASS = "observed"

#: A stable, descriptive identity for the process that runs this probe --
#: not a claim that a Kubernetes object of this shape exists anywhere.
#: ArchObservedWorkload requires non-blank cluster/namespace/kind/name;
#: mirrors activities/worker_revision_drift.py::WORKLOAD's own rationale.
WORKLOAD = {
    "cluster": "factory",
    "namespace": "factory-dispatcher",
    "kind": "Process",
    "name": "probe-console-surface",
}

#: Per apps/substrate/src/bead_rules.py::STATE_MACHINES[("arch","release")]:
#: the only two states with no outgoing edge. Mirrors apps/lifeops-console/
#: src/lib/release-index.ts::TERMINAL_RELEASE_STATES.
TERMINAL_RELEASE_STATES = frozenset({"released", "abandoned"})

#: What this probe does NOT check, recorded on every observation so a reader
#: never mistakes silence for coverage. Fixed, not dated -- see this
#: module's own docstring for why each entry is out of reach.
NOT_PROBED = (
    "factory-state panel queue counts (factory-status.ts::queueCountsFrom, "
    "read over the substrate proxy -- no machine token holds substrate.proxy)",
    "factory-state panel worker liveness (obs.worker-revision-drift, read "
    "over the substrate proxy)",
    "release index browser-computed outcome counts (release-index.ts::classifyOutcome)",
    "rendering",
    "the Cloudflare Access / ForwardAuth hop in front of the gateway",
)


def is_open_release_state(state: str) -> bool:
    """Non-terminal -- this probe checks what the console shows for a
    release still on the board, and a closing release is still on it
    (wider than dispatch.py's OPEN_RELEASE_STATES, which excludes
    "closing")."""
    return state not in TERMINAL_RELEASE_STATES


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Divergence:
    group: str
    field: str
    claim: Any
    authority: Any


@dataclass(frozen=True)
class GroupResult:
    """One group's own verdict -- "schedule" or "release[<ref>]" -- so a
    close note can say one group was demonstrated on a day another was
    cannot-evaluate."""

    group: str
    outcome: str  # "demonstrated" | "failed:<step>" | "cannot-evaluate:<reason>"
    divergences: list[Divergence] = field(default_factory=list)
    detail: str = ""


@dataclass(frozen=True)
class ProbeResult:
    probe_at: str
    ran_by: str
    probed_revision: str
    operations: list[str]
    release_refs: list[str]
    schedule: GroupResult
    releases: dict[str, GroupResult]
    outcome: str  # aggregate over every group
    divergences: list[Divergence]
    detail: str = ""


class ProbeCannotEvaluate(RuntimeError):
    """PRIN-015: the probe could not evaluate its question at all (an
    unresolvable release ref enumeration) -- distinct from a definite
    failure, and never reported as ``demonstrated``."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------


class HttpJsonClient(Protocol):
    """A JSON-over-HTTP port against the gateway's own two routes. The real
    implementation wraps httpx against mcp-hub itself, never the substrate
    proxy a browser also reaches -- see this module's own docstring."""

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any: ...

    async def post_json(self, path: str, json: dict[str, Any] | None = None) -> Any: ...


class AuthorityStore(Protocol):
    """The shared substrate client's (apps/substrate/client, loaded via
    substrate_client_loader) read AND write surface this module needs."""

    def find_bead(self, namespace: str, type: str, content_ref: str) -> dict[str, Any] | None: ...

    def list_beads(
        self, namespace: str, type: str, state: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]: ...

    def list_tasks(self, state: str | None = None, limit: int = 200) -> list[dict[str, Any]]: ...

    def list_links(
        self, bead_id: str, *, direction: str = "both", link_type: str | None = None
    ) -> list[dict[str, Any]]: ...

    def create_bead(
        self,
        namespace: str,
        type: str,
        state: str,
        content: dict[str, Any],
        created_by: str,
        *,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def patch_content(self, bead_id: str, content: dict[str, Any], created_by: str) -> dict[str, Any]: ...

    def patch_context(self, bead_id: str, context: dict[str, Any], created_by: str) -> dict[str, Any]: ...


class ScheduleAuthority(Protocol):
    async def describe(self, schedule_id: str) -> tuple[bool, str | None, list[str]]:
        """(paused, note, in_flight_workflow_ids) read directly from
        Temporal's own schedule description -- a different code path from
        the gateway's own schedule_status route (schedule_runtime.py),
        even though both ultimately ask the same schedule."""
        ...


# ---------------------------------------------------------------------------
# Schedule group -- AC-1 groups.schedule
# ---------------------------------------------------------------------------


def _check(
    divergences: list[Divergence], group: str, name: str, claim_value: Any, authority_value: Any
) -> None:
    if claim_value != authority_value:
        divergences.append(Divergence(group, name, claim_value, authority_value))


async def _probe_schedule(
    gateway: HttpJsonClient,
    schedule_authority: ScheduleAuthority,
    schedule_id: str,
    ops: list[str],
) -> GroupResult:
    ops.append("GET /api/v1/factory/schedule_status")
    try:
        schedule = await gateway.get_json("/api/v1/factory/schedule_status")
    except Exception as exc:  # noqa: BLE001 - an unreachable gateway is a claim-step failure
        return GroupResult("schedule", "failed:read_claims", detail=str(exc))

    if not isinstance(schedule, dict) or schedule.get("status") != "ok":
        detail = schedule.get("detail") if isinstance(schedule, dict) else None
        return GroupResult(
            "schedule", "failed:read_claims", detail=f"schedule_status did not answer ok: {detail!r}"
        )

    claim_paused = schedule.get("paused")
    claim_note = schedule.get("note")
    claim_in_flight = sorted(
        {wf.get("workflow_id") for wf in (schedule.get("in_flight") or []) if wf.get("workflow_id")}
    )

    try:
        paused, note, in_flight_ids = await schedule_authority.describe(schedule_id)
    except Exception as exc:  # noqa: BLE001 - collapsed into a declared step failure
        return GroupResult(
            "schedule", "failed:read_authorities", detail=f"Temporal describe failed: {exc}"
        )

    divergences: list[Divergence] = []
    _check(divergences, "schedule", "schedule.paused", claim_paused, paused)
    _check(divergences, "schedule", "schedule.pause_note", claim_note, note)
    _check(divergences, "schedule", "schedule.in_flight", claim_in_flight, sorted(in_flight_ids))

    if divergences:
        return GroupResult(
            "schedule",
            "failed:compare",
            divergences,
            detail=f"{len(divergences)} field(s) diverged between the console's claim and its authorities",
        )
    return GroupResult("schedule", "demonstrated")


# ---------------------------------------------------------------------------
# Release group -- AC-1 groups.releases[<ref>]
# ---------------------------------------------------------------------------


def _sorted_state_map(tasks_by_state: dict[str, list[str]]) -> dict[str, list[str]]:
    return {state: sorted(ids) for state, ids in tasks_by_state.items()}


async def _probe_release(gateway: HttpJsonClient, store: AuthorityStore, ref: str, ops: list[str]) -> GroupResult:
    group = f"release[{ref}]"
    ops.append(f"POST /api/v1/factory/release_delivery {ref}")
    try:
        response = await gateway.post_json("/api/v1/factory/release_delivery", json={"release_ref": ref})
    except Exception as exc:  # noqa: BLE001
        return GroupResult(group, "failed:read_claims", detail=str(exc))

    if not isinstance(response, dict):
        return GroupResult(group, "failed:read_claims", detail=f"release_delivery returned {response!r}")

    if response.get("status") == "unknown":
        # OPS-110 arm (c): every production deployment answers not_configured
        # here -- a permanent, by-design absence, never a zero (PRIN-015).
        code = response.get("code") or "unknown"
        reason = "not_configured" if code == "not_configured" else f"release_delivery_{code}"
        return GroupResult(group, f"cannot-evaluate:{reason}", detail=str(response.get("detail") or ""))

    if response.get("status") != "ok":
        return GroupResult(group, "failed:read_claims", detail=f"release_delivery did not answer ok: {response!r}")

    if response.get("found") is False:
        return GroupResult(group, "cannot-evaluate:no-such-release")

    claim_outcomes = {
        outcome["id"]: {
            "task_count": outcome.get("task_count"),
            "tasks_by_state": _sorted_state_map(outcome.get("tasks_by_state") or {}),
        }
        for outcome in response.get("outcomes") or []
    }
    claim_unclassified = sorted(response.get("unclassified_delivering") or [])

    try:
        charter = store.find_bead("arch", "release", ref)
        if charter is None:
            # The claim's authority (git charter files, via release_delivery)
            # and this probe's authority (the arch.release bead REL-1 mirrors
            # from them) are deliberately different populations -- a charter
            # with no mirrored bead yet cannot be independently checked at
            # all.
            return GroupResult(group, "cannot-evaluate:no-release-bead")

        links = store.list_links(charter["id"], direction="incoming", link_type="delivers")
        tasks = store.list_tasks(limit=200)
    except Exception as exc:  # noqa: BLE001 - an unreachable store or a store error is a step failure, not a 500
        return GroupResult(group, "failed:read_authorities", detail=f"authority read failed: {exc}")

    delivering_ids = {link["source_id"] for link in links if link.get("link_type") == "delivers"}
    outcomes_by_id = {outcome["id"]: outcome for outcome in (charter.get("content") or {}).get("outcomes") or []}

    authority_outcomes: dict[str, dict[str, Any]] = {}
    for outcome_id in outcomes_by_id:
        tasks_by_state: dict[str, list[str]] = {}
        for task in tasks:
            if task.get("id") in delivering_ids and (task.get("content") or {}).get("outcome_ref") == outcome_id:
                tasks_by_state.setdefault(task.get("state") or "(unknown)", []).append(task["id"])
        authority_outcomes[outcome_id] = {
            "task_count": sum(len(ids) for ids in tasks_by_state.values()),
            "tasks_by_state": _sorted_state_map(tasks_by_state),
        }
    authority_unclassified = sorted(
        task["id"]
        for task in tasks
        if task.get("id") in delivering_ids and (task.get("content") or {}).get("outcome_ref") not in outcomes_by_id
    )

    divergences: list[Divergence] = []
    for outcome_id in sorted(set(claim_outcomes) | set(authority_outcomes)):
        claim_outcome = claim_outcomes.get(outcome_id)
        authority_outcome = authority_outcomes.get(outcome_id)
        _check(
            divergences,
            group,
            f"{group}.outcomes[{outcome_id}].task_count",
            claim_outcome.get("task_count") if claim_outcome else None,
            authority_outcome.get("task_count") if authority_outcome else None,
        )
        _check(
            divergences,
            group,
            f"{group}.outcomes[{outcome_id}].tasks_by_state",
            claim_outcome.get("tasks_by_state") if claim_outcome else None,
            authority_outcome.get("tasks_by_state") if authority_outcome else None,
        )
    _check(divergences, group, f"{group}.unclassified_delivering", claim_unclassified, authority_unclassified)

    if divergences:
        return GroupResult(
            group,
            "failed:compare",
            divergences,
            detail=f"{len(divergences)} field(s) diverged between the console's claim and its authorities",
        )
    return GroupResult(group, "demonstrated")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _iso(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _default_release_refs(store: AuthorityStore) -> list[str]:
    releases = store.list_beads("arch", "release")
    return sorted(
        {
            ref
            for release in releases
            if is_open_release_state(release.get("state") or "")
            for ref in [(release.get("content") or {}).get("ref")]
            if ref
        }
    )


def _aggregate_outcome(schedule: GroupResult, releases: dict[str, GroupResult]) -> str:
    """``demonstrated`` only when every group evaluated found zero
    divergences and none was cannot-evaluate (AC-3/PRIN-015). A definite
    failure anywhere outranks an indeterminate one for the aggregate string,
    since it is the stronger claim -- both are equally "not demonstrated"."""
    flat = {"schedule": schedule.outcome, **{f"release[{ref}]": g.outcome for ref, g in releases.items()}}
    failing = sorted(g for g, outcome in flat.items() if outcome.startswith("failed:"))
    if failing:
        return "failed:" + ",".join(failing)
    uncertain = sorted(g for g, outcome in flat.items() if outcome.startswith("cannot-evaluate:"))
    if uncertain:
        return "cannot-evaluate:" + ",".join(uncertain)
    return "demonstrated"


async def run_probe(
    *,
    gateway: HttpJsonClient,
    store: AuthorityStore,
    schedule_authority: ScheduleAuthority,
    schedule_id: str,
    release_refs: list[str] | None,
    ran_by: str,
    probed_revision: str,
    now: datetime | None = None,
) -> ProbeResult:
    """The sequence as data: per group, (1) claim, (2) authority, (3)
    compare. The aggregate ``outcome`` is ``demonstrated`` only when every
    group is (AC-3) -- never inferred from a write's status code."""
    probe_at = _iso(now or datetime.now(timezone.utc))
    ops: list[str] = []

    if release_refs is None:
        try:
            release_refs = _default_release_refs(store)
        except Exception as exc:  # noqa: BLE001
            empty_schedule = GroupResult("schedule", "cannot-evaluate:release-enumeration-failed")
            return ProbeResult(
                probe_at=probe_at,
                ran_by=ran_by,
                probed_revision=probed_revision,
                operations=[],
                release_refs=[],
                schedule=empty_schedule,
                releases={},
                outcome="cannot-evaluate:release-enumeration-failed",
                divergences=[],
                detail=str(exc),
            )

    schedule_result = await _probe_schedule(gateway, schedule_authority, schedule_id, ops)

    release_results: dict[str, GroupResult] = {}
    for ref in release_refs:
        release_results[ref] = await _probe_release(gateway, store, ref, ops)

    all_divergences = list(schedule_result.divergences)
    for group_result in release_results.values():
        all_divergences.extend(group_result.divergences)

    detail = "; ".join(
        d for d in [schedule_result.detail, *(g.detail for g in release_results.values())] if d
    )

    return ProbeResult(
        probe_at=probe_at,
        ran_by=ran_by,
        probed_revision=probed_revision,
        operations=ops,
        release_refs=release_refs,
        schedule=schedule_result,
        releases=release_results,
        outcome=_aggregate_outcome(schedule_result, release_results),
        divergences=all_divergences,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Recorder -- find_bead, then create_bead(context=...) on the first run or
# patch_content followed by patch_context on every later one (AC-2: two
# PATCHes, so neither is ever optimised away -- observed_at changes every
# run, so the closed content is re-validated by the store every time).
# ---------------------------------------------------------------------------


def _content(observed_at_iso: str) -> dict[str, Any]:
    return {
        "ref": OBSERVATION_REF,
        "observed_at": observed_at_iso,
        "workload": dict(WORKLOAD),
        "source_class": OBSERVATION_SOURCE_CLASS,
    }


def _context(result: ProbeResult) -> dict[str, Any]:
    return {
        "probe_at": result.probe_at,
        "probed_revision": result.probed_revision,
        "ran_by": result.ran_by,
        "operations": result.operations,
        "release_refs": result.release_refs,
        "groups": {
            "schedule": result.schedule.outcome,
            "releases": {ref: group.outcome for ref, group in result.releases.items()},
        },
        "outcome": result.outcome,
        "not_probed": list(NOT_PROBED),
        "divergences": [
            {"group": d.group, "field": d.field, "claim": d.claim, "authority": d.authority}
            for d in result.divergences
        ],
    }


def record_observation(client: AuthorityStore, result: ProbeResult) -> dict[str, Any]:
    """Updated in place by content_ref lookup, never minted per run."""
    content = _content(result.probe_at)
    context = _context(result)
    existing = client.find_bead("arch", "observation", OBSERVATION_REF)
    if existing is None:
        return client.create_bead("arch", "observation", "active", content, CREATED_BY, context=context)
    patched = client.patch_content(existing["id"], content, CREATED_BY)
    return client.patch_context(patched["id"], context, CREATED_BY)


async def run_and_record(
    *,
    gateway: HttpJsonClient,
    store: AuthorityStore,
    schedule_authority: ScheduleAuthority,
    schedule_id: str,
    release_refs: list[str] | None,
    ran_by: str,
    probed_revision: str,
    recorder: AuthorityStore | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The one implementation both the CLI and the gateway route call."""
    result = await run_probe(
        gateway=gateway,
        store=store,
        schedule_authority=schedule_authority,
        schedule_id=schedule_id,
        release_refs=release_refs,
        ran_by=ran_by,
        probed_revision=probed_revision,
        now=now,
    )
    try:
        bead = record_observation(recorder or store, result)
    except Exception as exc:  # noqa: BLE001 - a refused write or an unreachable store is reported, never a 500
        # Two shapes of store-communication failure, neither ever a real bug:
        # a refused write, matched on .status (never on the class: the
        # dispatcher's own SubstrateError (substrate.py) and the packaged
        # client's (apps/substrate/client) are two distinct classes that both
        # carry this attribute -- isinstance against either would miss the
        # other); and an unreachable store, raised by httpx's own transport
        # before any HTTP response exists, so it carries no .status at all.
        # Anything else is a real bug and must not be hidden.
        import httpx

        if not hasattr(exc, "status") and not isinstance(exc, httpx.HTTPError):
            raise
        recorded_result = dataclasses.replace(
            result, outcome="failed:record", detail=f"observation write failed: {exc}"
        )
        return {"result": recorded_result, "bead": None}
    return {"result": result, "bead": bead}


# ---------------------------------------------------------------------------
# Real port implementations -- shared by the CLI (below) and
# apps/mcp-hub/src/tools/factory_probe.py, so "runnable two ways" stays one
# implementation all the way down to the HTTP/Temporal plumbing, not just the
# orchestration function.
# ---------------------------------------------------------------------------


class HttpxJsonClient:
    """Real ``HttpJsonClient`` -- a plain ``httpx`` call against the
    gateway's own ``base_url``. Never pointed at the substrate proxy -- see
    this module's own docstring for why the claim step reads only the
    gateway's two routes."""

    def __init__(self, base_url: str, headers: dict[str, str]):
        self._base_url = base_url.rstrip("/")
        self._headers = headers

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        import httpx

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(f"{self._base_url}{path}", params=params, headers=self._headers)
            resp.raise_for_status()
            return resp.json()

    async def post_json(self, path: str, json: dict[str, Any] | None = None) -> Any:
        import httpx

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{self._base_url}{path}", json=json, headers=self._headers)
            resp.raise_for_status()
            return resp.json()


def _workflow_id(action: Any) -> str:
    action_detail = getattr(action, "action", action)
    workflow_id = getattr(action, "workflow_id", "") or getattr(action_detail, "workflow_id", "")
    return str(workflow_id) if workflow_id else ""


class TemporalScheduleAuthority:
    """Real ``ScheduleAuthority`` -- reads Temporal's own schedule
    description directly, independent of the gateway's HTTP wrapper
    (schedule_runtime.describe_factory_schedule_status / tools/
    factory_schedule_status.py, which this module deliberately does not
    import: importing the gateway's own reader would make step 2 re-derive
    step 1 rather than check it)."""

    def __init__(self, client: Any):
        self._client = client

    async def describe(self, schedule_id: str) -> tuple[bool, str | None, list[str]]:
        description = await self._client.get_schedule_handle(schedule_id).describe()
        state = description.schedule.state
        info = getattr(description, "info", None)
        running_actions = getattr(info, "running_actions", ()) or ()
        in_flight_ids = sorted({wf_id for a in running_actions if (wf_id := _workflow_id(a))})
        return bool(state.paused), getattr(state, "note", None), in_flight_ids


def git_revision(repo_dir: Path) -> str:
    """The git revision of ``repo_dir``'s checkout, best-effort. Never
    raises: an unreadable revision is reported honestly in the observation
    rather than blocking a run that otherwise completed."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            env=process_env.child_env(),
        )
    except Exception:  # noqa: BLE001
        return "(unknown)"
    if completed.returncode != 0:
        return "(unknown)"
    return completed.stdout.strip() or "(unknown)"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_store() -> AuthorityStore:
    from substrate_client_loader import Substrate as SharedSubstrate

    return SharedSubstrate()


#: The gateway's identity-header NAMES are that layer's own configuration
#: (apps/mcp-hub/src/access_auth.py), never a literal compiled into this
#: file: apps/factory-dispatcher/*.py is scanned by
#: scripts/check-repo-invariants.py's no-household-identifiers rule
#: (R2603-5, HOUSEHOLD_IDENTIFIER_CORE_GLOBS). A JSON object of header name
#: -> value; any value containing the literal token "CLIENT_IDENTITY" has it
#: substituted with this run's own client identity.
GATEWAY_IDENTITY_HEADERS_ENV = "FACTORY_PROBE_GATEWAY_HEADERS"


def _build_gateway(gateway_url: str, client_identity: str) -> HttpJsonClient:
    raw = os.environ.get(GATEWAY_IDENTITY_HEADERS_ENV)
    if not raw:
        raise RuntimeError(
            f"{GATEWAY_IDENTITY_HEADERS_ENV} is not set -- the gateway's identity "
            "header names are configuration this module refuses to default (R2603-5). "
            'Set it to a JSON object, e.g. \'{"X-Client": "CLIENT_IDENTITY", '
            '"X-Client-Type": "service", "X-Scopes": "factory.read"}\'.'
        )
    headers = {
        name: value.replace("CLIENT_IDENTITY", client_identity)
        for name, value in json.loads(raw).items()
    }
    return HttpxJsonClient(gateway_url, headers)


async def _cli_run(args: argparse.Namespace) -> dict[str, Any]:
    from temporalio.client import Client as TemporalClient

    store = _build_store()
    gateway = _build_gateway(args.gateway_url, args.client_identity)
    probed_revision = git_revision(Path(__file__).resolve().parent)

    temporal_address = os.environ.get(
        "TEMPORAL_ADDRESS", "temporal.platform-core.svc.cluster.local:7233"
    )
    temporal_namespace = os.environ.get("FACTORY_TEMPORAL_NAMESPACE", "dev")
    temporal_client = await TemporalClient.connect(temporal_address, namespace=temporal_namespace)
    schedule_authority = TemporalScheduleAuthority(temporal_client)
    schedule_id = os.environ.get("FACTORY_DISPATCH_SCHEDULE_ID", "factory-dispatcher-dev")

    return await run_and_record(
        gateway=gateway,
        store=store,
        schedule_authority=schedule_authority,
        schedule_id=schedule_id,
        release_refs=args.release_ref,
        ran_by=args.client_identity,
        probed_revision=probed_revision,
        recorder=store,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--release-ref",
        action="append",
        default=None,
        help="Release ref to check (repeatable); omit for every non-terminal release",
    )
    parser.add_argument(
        "--gateway-url",
        default=os.environ.get("FACTORY_PROBE_GATEWAY_URL", "http://localhost:8000"),
    )
    parser.add_argument(
        "--client-identity",
        default=os.environ.get("FACTORY_PROBE_CLIENT_IDENTITY", "factory-dispatcher/probe-console-surface-cli"),
    )
    args = parser.parse_args(argv)

    outcome = asyncio.run(_cli_run(args))
    result: ProbeResult = outcome["result"]
    bead = outcome["bead"]
    print(
        json.dumps(
            {
                "outcome": result.outcome,
                "probe_at": result.probe_at,
                "probed_revision": result.probed_revision,
                "checked_releases": result.release_refs,
                "groups": {
                    "schedule": result.schedule.outcome,
                    "releases": {ref: g.outcome for ref, g in result.releases.items()},
                },
                "not_probed": list(NOT_PROBED),
                "divergences": [
                    {"group": d.group, "field": d.field, "claim": d.claim, "authority": d.authority}
                    for d in result.divergences
                ],
                "detail": result.detail,
                "observation_bead_id": bead.get("id") if bead else None,
            },
            indent=2,
        )
    )
    return 0 if result.outcome == "demonstrated" else 1


if __name__ == "__main__":
    sys.exit(main())
