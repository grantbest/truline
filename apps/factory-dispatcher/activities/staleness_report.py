"""Nightly requirement verdict staleness observations.

This activity reads requirement registries and writes ``arch.observation`` beads.
It deliberately never writes requirement files and never files ``dev.task`` work:
freshness is a measurement stream, not a structure edit or work generator.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import httpx
from temporalio import activity

import scanner

REPO_ROOT = Path(__file__).resolve().parents[3]

from substrate_client_loader import Substrate as _SharedSubstrateClient  # noqa: E402

REGISTRIES_DIR = REPO_ROOT / "docs" / "requirements"
RELEASES_DIR = REPO_ROOT / "docs" / "releases"
CREATED_BY = "factory-dispatcher/staleness-report"
# "factory-dispatcher/staleness-report" is enrolled in
# apps/substrate/src/bead_rules.py's SOURCE_CLASS_WRITERS["derived"] (OPS-119,
# #822) -- declaring "derived" here depends on that enrolment being DEPLOYED,
# not just merged (unenrolled, this would be a 409). See .factory/design.md.
SOURCE_CLASS = "derived"
OBSERVATION_KIND_STALE = "requirement_verdict_staleness"
OBSERVATION_KIND_SUMMARY = "requirement_verdict_staleness_summary"
HTTP_TIMEOUT_S = 30.0


class ObservationStore(Protocol):
    def observation_exists(self, ref: str) -> bool:
        ...

    def create_observation(self, payload: dict[str, Any]) -> dict[str, Any]:
        ...


class SubstrateObservationStore:
    """Narrow client for the arch observation writes this report owns."""

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        # Header construction and credential resolution live in substrate_client
        # (the one Python substrate client, M6) rather than duplicated here.
        _client = _SharedSubstrateClient(base_url=base_url, api_key=api_key)
        self.base_url = _client.base_url
        self._headers = _client._headers

    def _request(
        self, method: str, path: str, headers: dict[str, str] | None = None, **kwargs: Any
    ) -> Any:
        merged_headers = {**self._headers, **(headers or {})}
        response = httpx.request(
            method,
            f"{self.base_url}{path}",
            headers=merged_headers,
            timeout=HTTP_TIMEOUT_S,
            **kwargs,
        )
        response.raise_for_status()
        return response.json()

    def observation_exists(self, ref: str) -> bool:
        found = self._request(
            "GET",
            "/beads",
            params={
                "namespace": "arch",
                "type": "observation",
                "content_ref": ref,
                "limit": 1,
            },
        )
        return bool(found)

    def create_observation(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/beads", json=payload)


@dataclass(frozen=True)
class StaleVerdict:
    registry_id: str
    registry_path: str
    requirement_id: str
    criterion_id: str
    conformance: str
    measured_revision: str
    invalidating_revision: str
    reason: str

    @property
    def requirement_ref(self) -> str:
        return f"{self.requirement_id}/{self.criterion_id}"


@dataclass(frozen=True)
class StalenessReport:
    checked: int
    stale: tuple[StaleVerdict, ...]


def collect_staleness_report(
    registries_dir: Path = REGISTRIES_DIR,
    *,
    last_changed_revision_fn: Callable[[tuple[str, ...]], str | None] = (
        scanner.last_changed_revision
    ),
    revision_exists_fn: Callable[[str], bool] = scanner.revision_exists,
    is_ancestor_fn: Callable[[str, str], bool] = scanner.is_ancestor,
) -> StalenessReport:
    """Compare every revision-measured registry verdict to the paths it judges."""
    checked = 0
    stale: list[StaleVerdict] = []

    for path in sorted(registries_dir.glob("*.json")):
        if path.name.endswith(".schema.json"):
            continue
        registry = json.loads(path.read_text())
        registry_id = str((registry.get("registry") or {}).get("id") or path.stem)
        for requirement in registry.get("requirements") or []:
            paths = scanner._implementation_paths(requirement)
            revision_criteria = [
                ac
                for ac in requirement.get("acceptance_criteria") or []
                if ac.get("conformance") is not None
                and str(ac.get("measured_revision") or "").strip()
            ]
            if not revision_criteria:
                continue
            last_touching_revision = last_changed_revision_fn(paths)
            for criterion in revision_criteria:
                checked += 1
                measured_revision = str(criterion.get("measured_revision")).strip()
                reason = scanner._revision_stale_reason(
                    measured_revision,
                    last_touching_revision,
                    paths,
                    revision_exists_fn,
                    is_ancestor_fn,
                )
                if not reason:
                    continue
                stale.append(
                    StaleVerdict(
                        registry_id=registry_id,
                        registry_path=_display_path(path),
                        requirement_id=str(requirement.get("id") or ""),
                        criterion_id=str(criterion.get("id") or ""),
                        conformance=str(criterion.get("conformance") or ""),
                        measured_revision=measured_revision,
                        invalidating_revision=last_touching_revision or "",
                        reason=reason,
                    )
                )

    return StalenessReport(checked=checked, stale=tuple(stale))


def resolve_cited_criteria(
    *,
    releases_dir: Path,
    requirements_dir: Path,
    reader: Any | None,
    release_load: Any,
    release_status: Any,
) -> frozenset[str] | None:
    """The ``{requirement_id}/{criterion_id}`` refs cited by every release not
    already in state released.

    Reuses ``release_status.build_release_report`` -- the same resolution
    scripts/release-status.py itself runs -- against an empty task/delivers/
    conformance population, since only the ``ref`` each ``CriterionStatus``
    carries is wanted here; nothing about a criterion's own conformance is
    read or inferred. Two calls into the same function cannot disagree about
    which criteria a release stands on.

    Returns ``None`` -- never an empty set standing in for it -- when there is
    no open release to ask (no charters at all, or every charter is already
    released), when the open population cites nothing, or when the charters
    cannot be loaded against ``requirements_dir`` at all (``load_charters``
    exits rather than returning a partial set, the same way
    ``scripts/release-status.py``'s own ``main()`` treats that failure as
    "could not run" rather than a crash): all of these are "no burndown
    target could be established", which is a different fact from "checked,
    and it is zero".
    """
    from activities.release_status import RELEASED_STATE, _release_states

    try:
        charter_items = release_load.load_charters(
            release_load.charter_paths(releases_dir),
            requirements_dir=requirements_dir,
        )
    except SystemExit:
        return None
    if not charter_items:
        return None

    states = _release_states(reader) if reader is not None else {}
    open_items = [
        item for item in charter_items if states.get(item.ref, "") != RELEASED_STATE
    ]
    if not open_items:
        return None

    cited: set[str] = set()
    for item in open_items:
        report = release_status.build_release_report(item.content, [], {}, [])
        cited.update(status.ref for status in report.criteria)
    return frozenset(cited) if cited else None


def run_staleness_report(
    store: ObservationStore,
    *,
    registries_dir: Path = REGISTRIES_DIR,
    releases_dir: Path = RELEASES_DIR,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    last_changed_revision_fn: Callable[[tuple[str, ...]], str | None] = (
        scanner.last_changed_revision
    ),
    revision_exists_fn: Callable[[str], bool] = scanner.revision_exists,
    is_ancestor_fn: Callable[[str, str], bool] = scanner.is_ancestor,
    reader: Any | None = None,
    release_load: Any | None = None,
    release_status: Any | None = None,
) -> dict[str, Any]:
    """Emit stale-verdict observations plus one summary observation."""
    observed_at = _observed_at(now_fn())
    observed_day = observed_at[:10]
    report = collect_staleness_report(
        registries_dir,
        last_changed_revision_fn=last_changed_revision_fn,
        revision_exists_fn=revision_exists_fn,
        is_ancestor_fn=is_ancestor_fn,
    )

    created = 0
    skipped_existing = 0
    for verdict in report.stale:
        emitted = _emit_once(
            store,
            _stale_observation_payload(verdict, observed_at, observed_day),
        )
        created += int(emitted)
        skipped_existing += int(not emitted)

    if release_status is None:
        from activities.release_status import _load_release_status_module

        release_status = _load_release_status_module()
    if release_load is None:
        release_load = release_status._load_release_load_module()

    cited_refs = resolve_cited_criteria(
        releases_dir=releases_dir,
        requirements_dir=registries_dir,
        reader=reader,
        release_load=release_load,
        release_status=release_status,
    )
    cited_stale = (
        None
        if cited_refs is None
        else sum(1 for v in report.stale if v.requirement_ref in cited_refs)
    )

    summary = _summary_observation_payload(report, cited_stale, observed_at, observed_day)
    emitted = _emit_once(store, summary)
    created += int(emitted)
    skipped_existing += int(not emitted)

    return {
        "status": "reported",
        "checked": report.checked,
        "stale": len(report.stale),
        "cited_stale": cited_stale,
        "observations_created": created,
        "observations_skipped_existing": skipped_existing,
    }


@activity.defn(name="report_verdict_staleness")
def report_verdict_staleness_activity(request: dict[str, Any]) -> dict[str, Any]:
    request = request or {}
    from activities.release_status import _load_release_status_module

    release_status = _load_release_status_module()
    reader = release_status.GateSubstrateReader(
        os.environ.get("SUBSTRATE_URL"), os.environ.get("SUBSTRATE_API_KEY")
    )
    return run_staleness_report(
        SubstrateObservationStore(),
        registries_dir=Path(request.get("registries_dir") or REGISTRIES_DIR),
        releases_dir=Path(request.get("releases_dir") or RELEASES_DIR),
        reader=reader,
        release_status=release_status,
    )


def _emit_once(store: ObservationStore, payload: dict[str, Any]) -> bool:
    ref = str((payload.get("content") or {}).get("ref") or "")
    if store.observation_exists(ref):
        return False
    store.create_observation(payload)
    return True


def _stale_observation_payload(
    verdict: StaleVerdict,
    observed_at: str,
    observed_day: str,
) -> dict[str, Any]:
    ref = ".".join(
        (
            "obs",
            "verdict-staleness",
            _ref_part(observed_day),
            _ref_part(verdict.registry_id),
            _ref_part(verdict.requirement_id),
            _ref_part(verdict.criterion_id),
            "measured",
            _ref_part(verdict.measured_revision),
            "invalidated",
            _ref_part(verdict.invalidating_revision or "unknown"),
        )
    )
    return {
        "namespace": "arch",
        "type": "observation",
        "state": "active",
        "trust_tier": "system",
        "created_by": CREATED_BY,
        "content": {
            "ref": ref,
            "source_class": SOURCE_CLASS,
            "observed_at": observed_at,
            "workload": {
                "cluster": "repository",
                "namespace": "requirements",
                "kind": "RequirementAcceptanceCriterion",
                "name": verdict.requirement_ref,
            },
        },
        "context": {
            "observation_kind": OBSERVATION_KIND_STALE,
            "registry_id": verdict.registry_id,
            "registry_path": verdict.registry_path,
            "requirement_id": verdict.requirement_id,
            "criterion_id": verdict.criterion_id,
            "requirement_ref": verdict.requirement_ref,
            "conformance": verdict.conformance,
            "measured_revision": verdict.measured_revision,
            "invalidating_revision": verdict.invalidating_revision,
            "reason": verdict.reason,
        },
    }


def _summary_observation_payload(
    report: StalenessReport,
    cited_stale: int | None,
    observed_at: str,
    observed_day: str,
) -> dict[str, Any]:
    ref = ".".join(
        (
            "obs",
            "verdict-staleness-summary",
            _ref_part(observed_day),
            f"checked-{report.checked}",
            f"stale-{len(report.stale)}",
        )
    )
    return {
        "namespace": "arch",
        "type": "observation",
        "state": "active",
        "trust_tier": "system",
        "created_by": CREATED_BY,
        "content": {
            "ref": ref,
            "source_class": SOURCE_CLASS,
            "observed_at": observed_at,
            "workload": {
                "cluster": "repository",
                "namespace": "requirements",
                "kind": "RequirementVerdictStalenessSummary",
                "name": f"checked-{report.checked}-stale-{len(report.stale)}",
            },
        },
        "context": {
            "observation_kind": OBSERVATION_KIND_SUMMARY,
            "total_checked": report.checked,
            "total_stale": len(report.stale),
            # A burndown target and a backlog size are different numbers.
            # None means no open release cites anything to burn down -- not
            # zero, which would read as a burndown already complete.
            "cited_stale": cited_stale,
        },
    }


def _observed_at(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _ref_part(value: str) -> str:
    cleaned = "".join(
        ch.lower() if ch.isalnum() else "-"
        for ch in str(value).strip()
        if ch.isalnum() or ch in ".-_"
    ).strip("-")
    return cleaned or "unknown"
