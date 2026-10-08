"""Nightly EA coverage report: a trend, not a reading (PC-ASR-002; S53-4).

`scripts/ea-coverage.py` has always been able to measure EA linkage coverage; nothing has
ever run it on a schedule, so a maturity number was only ever as fresh as the last person
who remembered to run it by hand -- the same gap R26.01/O-9 closed for conformance
verdicts with a daily staleness report. This activity is the scheduled caller, riding
`DoctrineStalenessReportWorkflow`'s existing nightly credentialed schedule
(`factory-doctrine-staleness-nightly`) as a fifth leg rather than registering a new
Temporal Schedule -- the bead names that schedule explicitly, and this reconciler needs no
credential the doctrine-staleness worker does not already hold.

Two measurements are combined into one dated report:

* The four git-model linkage rows (`application.depends_on_authored`, `application.loc`,
  `application.realizes`, `capability.supports`) -- `ea-coverage.py`'s existing pure
  functions over `docs/architecture/model/*.yaml`, unchanged.
* The technology-layer row (`arch.ci` attribution, `application_depends_on_posture`
  known/assessed-none/unknown, plus the coherence case) -- read from the standing
  `obs.ea-observer-status` bead `activities/ea_observation.py` already writes every
  night, never a second kubectl/substrate collector. See .factory/design.md for why,
  and why an unmeasurable row reports `evaluated: false` rather than a fabricated
  zero (PRIN-015).

Unlike `ea_observation.py`'s own standing-observation pattern (one bead per condition,
updated in place forever), this lands ONE NEW dated `arch.observation` bead per row per
run -- the mint-per-measurement shape `arch.requirement_conformance` already uses
(`scripts/requirements-load.py`'s `_measurement_ref`, `measured_at` in the ref) -- so the
store holds a series a trend can be read off of. The ref carries the measurement DATE (day
granularity, matching `ea-coverage.py`'s own `observed_at` grain), so a new day's run always
mints a bead a previous day's cannot collide with, while a retry within the same day
reconciles the same bead rather than duplicating it.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import httpx
from temporalio import activity

_DISPATCHER_ROOT = Path(__file__).resolve().parents[1]
if str(_DISPATCHER_ROOT) not in sys.path:
    sys.path.insert(0, str(_DISPATCHER_ROOT))

from activities.doctrine_staleness import CREATED_BY as DOCTRINE_STALENESS_CREATED_BY  # noqa: E402
from activities.ea_observation import OBSERVER_STATUS_REF  # noqa: E402
from substrate_client_loader import Substrate as _SharedSubstrateClient  # noqa: E402

REPO_ROOT = _DISPATCHER_ROOT.parents[1]
EA_COVERAGE_PATH = REPO_ROOT / "scripts" / "ea-coverage.py"

#: "factory-dispatcher/ea-coverage" is not enrolled in
#: apps/substrate/src/bead_rules.py's SOURCE_CLASS_WRITERS -- that file is out of scope for
#: this change (apps/substrate/** is forbidden), the same constraint activities/ea_dependency.py
#: hit and documented. This reconciler is a fifth leg of DoctrineStalenessReportWorkflow, so it
#: reuses that leg's already-enrolled "derived" identity instead. See .factory/design.md.
CREATED_BY = DOCTRINE_STALENESS_CREATED_BY
SOURCE_CLASS = "derived"
OBSERVATION_KIND = "ea_coverage_measurement"
HTTP_TIMEOUT_S = 30.0


class EACoverageStore(Protocol):
    """Deliberately narrow: exactly what landing a dated coverage-row observation
    and reading the standing observer-status bead needs -- one tested consumer,
    three methods, nothing more (the same reasoning ``EAObserverStore`` and
    ``CIObserverStore`` already give for their own concern).
    """

    def find_observation(self, ref: str) -> dict[str, Any] | None: ...

    def create_observation(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def update_observation(
        self,
        bead_id: str,
        *,
        content: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


class SubstrateEACoverageStore:
    """Narrow client for the reads/writes this activity owns -- same REST shape
    as ``activities.ea_observation.SubstrateEAObserverStore``, not a reuse of it,
    since that class also carries CI/finding methods this activity never calls."""

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        _client = _SharedSubstrateClient(base_url=base_url, api_key=api_key)
        self.base_url = _client.base_url
        self._headers = _client._headers

    def _request(
        self, method: str, path: str, headers: dict[str, str] | None = None, **kwargs: Any
    ) -> Any:
        merged_headers = {**self._headers, **(headers or {})}
        response = httpx.request(
            method, f"{self.base_url}{path}", headers=merged_headers, timeout=HTTP_TIMEOUT_S, **kwargs
        )
        response.raise_for_status()
        return response.json()

    def find_observation(self, ref: str) -> dict[str, Any] | None:
        found = self._request(
            "GET",
            "/beads",
            params={"namespace": "arch", "type": "observation", "content_ref": ref, "limit": 1},
        )
        return found[0] if found else None

    def create_observation(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/beads", json=payload)

    def update_observation(
        self,
        bead_id: str,
        *,
        content: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"created_by": CREATED_BY}
        if content is not None:
            body["content"] = content
        if context is not None:
            body["context"] = context
        return self._request("PATCH", f"/beads/{bead_id}", json=body)


def default_store() -> EACoverageStore:
    return SubstrateEACoverageStore()


def load_ea_coverage_module() -> Any:
    """Import scripts/ea-coverage.py by path -- the filename is not a module
    name, same technique activities/requirements_apply.py already uses for
    scripts/requirements-load.py."""
    spec = importlib.util.spec_from_file_location("ea_coverage", EA_COVERAGE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _observed_at(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _ref_part(value: str) -> str:
    cleaned = "".join(
        ch.lower() if ch.isalnum() else "-"
        for ch in str(value).strip()
        if ch.isalnum() or ch in ".-_"
    ).strip("-")
    return cleaned or "unknown"


def _row_ref(field: str, measurement_date: str) -> str:
    """Day-granularity, mint-per-measurement (see module docstring): a new
    measurement date always names a bead the previous day's cannot collide
    with; the same date reconciles the same bead on a same-day retry."""
    return f"obs.ea-coverage.{_ref_part(field)}.{measurement_date}"


def _row_content(*, field: str, ref: str, observed_at: str) -> dict[str, Any]:
    """The closed ``ArchObservationContent`` shape only (``extra='forbid'``,
    apps/substrate/src/schemas.py) -- ``ref`` / ``source_class`` /
    ``observed_at`` / ``workload``, nothing measurement-specific. See
    .factory/design.md for why the row's own data lives in ``context``
    instead."""
    return {
        "ref": ref,
        "source_class": SOURCE_CLASS,
        "observed_at": observed_at,
        "workload": {
            "cluster": "repository",
            "namespace": "architecture",
            "kind": "EaCoverageRow",
            "name": field,
        },
    }


def _row_context(*, field: str, measurement_date: str, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "observation_kind": OBSERVATION_KIND,
        "field": field,
        "measurement_date": measurement_date,
        "row": row,
    }


def land_coverage_observation(
    store: EACoverageStore,
    observation: dict[str, Any],
    *,
    now_fn: Any = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Mint one dated bead per row, never overwritten across measurement dates
    (NFR-2, .factory/design.md): a run on a new ``observation["observed_at"]``
    date always creates a fresh bead per field; a retry on the same date finds
    and, only if the row actually changed, updates the same one -- the
    ``land_ci_records`` "zero writes when unchanged" discipline, not a second
    create.
    """
    measurement_date = observation["observed_at"]
    observed_at = _observed_at(now_fn())
    created = 0
    updated = 0
    unchanged = 0

    for field, row in observation["coverage"].items():
        ref = _row_ref(field, measurement_date)
        content = _row_content(field=field, ref=ref, observed_at=observed_at)
        context = _row_context(field=field, measurement_date=measurement_date, row=row)
        existing = store.find_observation(ref)
        if existing is None:
            store.create_observation(
                {
                    "namespace": "arch",
                    "type": "observation",
                    "state": "active",
                    "trust_tier": "system",
                    "created_by": CREATED_BY,
                    "content": content,
                    "context": context,
                }
            )
            created += 1
        elif existing.get("context", {}).get("row") != row:
            store.update_observation(existing["id"], content=content, context=context)
            updated += 1
        else:
            unchanged += 1

    return {
        "status": "reported",
        "measurement_date": measurement_date,
        "checked": len(observation["coverage"]),
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
    }


def report_ea_coverage(
    store: EACoverageStore,
    *,
    now_fn: Any = lambda: datetime.now(timezone.utc),
    ea_coverage_module: Any = None,
) -> dict[str, Any]:
    """Measure this run's coverage (four git-model rows plus the technology-layer
    row) and land it as a dated series. No kubectl, no KUBECONFIG: every read
    here is either the committed model or a bead ``ea_observation.py``'s
    separately-credentialed nightly run already wrote (NFR-3,
    .factory/design.md)."""
    eac = ea_coverage_module or load_ea_coverage_module()
    now = now_fn()

    capabilities = eac._load("business-layer.yaml", "capabilities")
    applications = eac._load("application-portfolio.yaml", "applications")
    observer_status = store.find_observation(OBSERVER_STATUS_REF)

    observation = eac.coverage_observation(
        eac.measure_model_coverage(capabilities, applications),
        observed_at=now.date().isoformat(),
        technology=eac.measure_technology_coverage(observer_status),
    )

    return land_coverage_observation(store, observation, now_fn=now_fn)


@activity.defn(name="report_ea_coverage")
def report_ea_coverage_activity(request: dict[str, Any] | None = None) -> dict[str, Any]:
    request = request or {}
    return report_ea_coverage(default_store())


ACTIVITIES = [report_ea_coverage_activity]
