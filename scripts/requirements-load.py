#!/usr/bin/env python3
"""Mirror requirement registries from git into the substrate.

`docs/requirements/*.json` stays authoritative for requirement structure. The
substrate gets an idempotent, queryable mirror:

* `arch.requirement` beads are keyed by `content.id`.
* Dated conformance measurements are removed from requirement content.
* Each measurement becomes an `arch.requirement_conformance` bead keyed by
  `content.ref` — never `arch.observation`, which is closed on `observed_at`
  and `workload` and has never accepted this shape.
* Dry-run is the default. Writes require `--apply`.
* No delete path exists in this loader.

Environment: SUBSTRATE_URL, SUBSTRATE_API_KEY.
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import pathlib
import sys
from typing import Any, NamedTuple, Optional

REPO = pathlib.Path(__file__).resolve().parent.parent
REQUIREMENTS_DIR = REPO / "docs" / "requirements"
SOURCES = (
    REQUIREMENTS_DIR / "platform-requirements.json",
    REQUIREMENTS_DIR / "lifeops-requirements.json",
)
REQUIREMENT_TYPE = "requirement"
CONFORMANCE_TYPE = "requirement_conformance"
CREATED_BY = "requirements-load"
MEASUREMENT_KEYS = frozenset(
    {"conformance", "verdict", "measured_at", "measured_revision"}
)

_SUBSTRATE_CLIENT_ALIAS = "_scripts_substrate_client_impl"


def _load_substrate_client_module():
    """Load scripts/substrate_client.py by file path, under an alias
    distinct from the bare name ``substrate_client``.

    apps/substrate/client/src/substrate_client is also importable as the
    bare name ``substrate_client`` and does not export ``SubstrateClient``;
    whichever of the two wins ``sys.modules["substrate_client"]`` first
    decides what a plain ``import substrate_client`` returns for the rest of
    the process (dev.finding d9ae4d93). Loading by path sidesteps that race
    entirely -- the same pattern
    apps/substrate/client/tests/test_public_surface.py already uses for the
    package's own suite. The alias is shared across every scripts/ caller of
    this module so a process that loads more than one of them (this repo's
    own test suite routinely does) executes it, and its apps/substrate/src
    schema imports, once rather than once per caller.
    """
    cached = sys.modules.get(_SUBSTRATE_CLIENT_ALIAS)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        _SUBSTRATE_CLIENT_ALIAS, pathlib.Path(__file__).resolve().parent / "substrate_client.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_SUBSTRATE_CLIENT_ALIAS] = module
    spec.loader.exec_module(module)
    return module


_substrate_client_module = _load_substrate_client_module()
_Substrate = _substrate_client_module.Substrate
SubstrateClient = _substrate_client_module.SubstrateClient
SubstrateError = _substrate_client_module.SubstrateError


class Substrate(_Substrate):
    def __init__(self) -> None:
        super().__init__(created_by=CREATED_BY)


class RequirementItem(NamedTuple):
    requirement_id: str
    state: str
    content: dict


class ConformanceItem(NamedTuple):
    requirement_id: str
    state: str
    content: dict


class Plan:
    """Counts describe outcomes, not attempts.

    In apply mode, `*_creates` / `*_updates` hold only ids the substrate
    accepted; a refused write lands in the matching `*_failures` list
    instead (and always gets a message in `errors`). In dry-run mode
    nothing is attempted, so `*_creates` / `*_updates` mean "planned" as
    before and the `*_failures` lists stay empty.
    """

    def __init__(self, apply: bool):
        self.apply = apply
        self.requirement_creates: list[str] = []
        self.requirement_create_failures: list[str] = []
        self.requirement_updates: list[str] = []
        self.requirement_update_failures: list[str] = []
        self.requirement_unchanged: list[str] = []
        self.conformance_creates: list[str] = []
        self.conformance_create_failures: list[str] = []
        self.conformance_updates: list[str] = []
        self.conformance_update_failures: list[str] = []
        self.conformance_unchanged: list[str] = []
        self.errors: list[str] = []

    @property
    def dry_run(self) -> bool:
        return not self.apply


def _strip_measurements(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_measurements(child)
            for key, child in value.items()
            if key not in MEASUREMENT_KEYS
        }
    if isinstance(value, list):
        return [_strip_measurements(child) for child in value]
    return value


def requirement_content(registry_id: str, requirement: dict) -> dict:
    """Return arch.requirement content with measurements removed.

    The LifeOps registry predates `ArchRequirementContent` and records intent in
    `user_story` instead of `rationale`. The substrate requires `rationale`, so
    the mirror uses the story text as the rationale while preserving `user_story`
    as authored.
    """
    content = _strip_measurements(copy.deepcopy(requirement))
    content["registry_id"] = registry_id
    if "rationale" not in content and "user_story" in content:
        content["rationale"] = content["user_story"]
    return content


def _measurement_ref(
    registry_id: str, requirement_id: str, criterion_id: Optional[str], measurement: dict
) -> str:
    parts = [
        "requirement",
        registry_id,
        requirement_id,
        criterion_id or "requirement",
        str(measurement.get("measured_at") or "undated"),
        str(measurement.get("measured_revision") or "unversioned"),
    ]
    return ".".join(part.replace("/", "-") for part in parts)


def conformance_content(
    registry_id: str,
    requirement: dict,
    measurement_source: dict,
    criterion_id: Optional[str] = None,
) -> dict:
    """Return arch.requirement_conformance content for one dated measurement.

    Flat, not nested: `release-status.py` reads these fields at the top level.
    `verdict` is the one substrate field for what the registries write
    inconsistently as `conformance` or `verdict` — normalized here so a reader
    never needs to know which name the source used.
    """
    verdict = measurement_source.get("conformance") or measurement_source.get("verdict")
    content = {
        "ref": _measurement_ref(registry_id, requirement["id"], criterion_id, measurement_source),
        "registry_id": registry_id,
        "requirement_id": requirement["id"],
        "measured_at": measurement_source.get("measured_at"),
        "verdict": verdict,
        "source_class": "derived",
    }
    if criterion_id is not None:
        content["acceptance_criterion_id"] = criterion_id
    if "verification" in measurement_source:
        content["verification"] = measurement_source["verification"]
    if "measured_revision" in measurement_source:
        content["measured_revision"] = measurement_source["measured_revision"]
    return content


def mapped_items(registry: dict) -> tuple[list[RequirementItem], list[ConformanceItem]]:
    registry_id = registry["registry"]["id"]
    requirements: list[RequirementItem] = []
    conformances: list[ConformanceItem] = []

    for requirement in registry.get("requirements", []):
        requirement_id = requirement["id"]
        state = requirement.get("status", "active")
        requirements.append(
            RequirementItem(
                requirement_id=requirement_id,
                state=state,
                content=requirement_content(registry_id, requirement),
            )
        )

        if MEASUREMENT_KEYS.intersection(requirement):
            conformances.append(
                ConformanceItem(
                    requirement_id=requirement_id,
                    state="active",
                    content=conformance_content(registry_id, requirement, requirement),
                )
            )

        for criterion in requirement.get("acceptance_criteria", []):
            if MEASUREMENT_KEYS.intersection(criterion):
                conformances.append(
                    ConformanceItem(
                        requirement_id=requirement_id,
                        state="active",
                        content=conformance_content(
                            registry_id, requirement, criterion, criterion.get("id")
                        ),
                    )
                )

    return requirements, conformances


def load_registries(paths: tuple[pathlib.Path, ...] = SOURCES) -> tuple[
    list[RequirementItem], list[ConformanceItem]
]:
    requirements: list[RequirementItem] = []
    conformances: list[ConformanceItem] = []
    seen: set[str] = set()
    for path in paths:
        registry = json.loads(path.read_text())
        next_requirements, next_conformances = mapped_items(registry)
        for item in next_requirements:
            if item.requirement_id in seen:
                sys.exit(f"duplicate requirement id: {item.requirement_id}")
            seen.add(item.requirement_id)
        requirements.extend(next_requirements)
        conformances.extend(next_conformances)
    return requirements, conformances


def _existing_by_requirement_id(sub: SubstrateClient) -> dict[str, dict]:
    existing: dict[str, dict] = {}
    for bead in sub.list_beads(REQUIREMENT_TYPE):
        requirement_id = (bead.get("content") or {}).get("id")
        if requirement_id:
            existing[requirement_id] = bead
    return existing


def _existing_by_conformance_ref(sub: SubstrateClient) -> dict[str, dict]:
    existing: dict[str, dict] = {}
    for bead in sub.list_beads(CONFORMANCE_TYPE):
        ref = (bead.get("content") or {}).get("ref")
        if ref:
            existing[ref] = bead
    return existing


def _safe_error(requirement_id: str, action: str, exc: SubstrateError) -> str:
    return f"{requirement_id}: {action} rejected: substrate {exc.status} on {exc.path}"


def reconcile(
    sub: SubstrateClient,
    requirements: list[RequirementItem],
    conformances: list[ConformanceItem],
    apply: bool = False,
) -> Plan:
    plan = Plan(apply=apply)
    existing_requirements = _existing_by_requirement_id(sub)
    existing_conformances = _existing_by_conformance_ref(sub)

    for item in requirements:
        current = existing_requirements.get(item.requirement_id)
        if current is None:
            if not apply:
                plan.requirement_creates.append(item.requirement_id)
                continue
            try:
                sub.create(REQUIREMENT_TYPE, item.state, item.content)
            except SubstrateError as exc:
                plan.requirement_create_failures.append(item.requirement_id)
                plan.errors.append(
                    _safe_error(item.requirement_id, "requirement create", exc)
                )
            else:
                plan.requirement_creates.append(item.requirement_id)
            continue

        body: dict[str, Any] = {}
        if current.get("content") != item.content:
            body["content"] = item.content
        if current.get("state") != item.state:
            body["state"] = item.state

        if not body:
            plan.requirement_unchanged.append(item.requirement_id)
        elif not apply:
            plan.requirement_updates.append(item.requirement_id)
        else:
            try:
                sub.patch(current["id"], body)
            except SubstrateError as exc:
                plan.requirement_update_failures.append(item.requirement_id)
                plan.errors.append(
                    _safe_error(item.requirement_id, "requirement update", exc)
                )
            else:
                plan.requirement_updates.append(item.requirement_id)

    for item in conformances:
        ref = item.content["ref"]
        current = existing_conformances.get(ref)
        if current is None:
            if not apply:
                plan.conformance_creates.append(ref)
                continue
            try:
                sub.create(CONFORMANCE_TYPE, item.state, item.content)
            except SubstrateError as exc:
                plan.conformance_create_failures.append(ref)
                plan.errors.append(
                    _safe_error(item.requirement_id, "conformance create", exc)
                )
            else:
                plan.conformance_creates.append(ref)
            continue

        body = {}
        if current.get("content") != item.content:
            body["content"] = item.content
        if current.get("state") != item.state:
            body["state"] = item.state

        if not body:
            plan.conformance_unchanged.append(ref)
        elif not apply:
            plan.conformance_updates.append(ref)
        else:
            try:
                sub.patch(current["id"], body)
            except SubstrateError as exc:
                plan.conformance_update_failures.append(ref)
                plan.errors.append(
                    _safe_error(item.requirement_id, "conformance update", exc)
                )
            else:
                plan.conformance_updates.append(ref)

    return plan


def _counts_line(
    label: str,
    creates: list[str],
    create_failures: list[str],
    updates: list[str],
    update_failures: list[str],
    unchanged: list[str],
) -> str:
    parts = [f"creates={len(creates)}"]
    if create_failures:
        parts.append(f"create_failures={len(create_failures)}")
    parts.append(f"updates={len(updates)}")
    if update_failures:
        parts.append(f"update_failures={len(update_failures)}")
    parts.append(f"unchanged={len(unchanged)}")
    return f"{label}: " + " ".join(parts)


def report(plan: Plan) -> int:
    head = "DRY RUN - nothing was written" if plan.dry_run else "applied"
    print(f"requirements-load: {head}")
    print(
        _counts_line(
            "requirements",
            plan.requirement_creates,
            plan.requirement_create_failures,
            plan.requirement_updates,
            plan.requirement_update_failures,
            plan.requirement_unchanged,
        )
    )
    print(
        _counts_line(
            "conformances",
            plan.conformance_creates,
            plan.conformance_create_failures,
            plan.conformance_updates,
            plan.conformance_update_failures,
            plan.conformance_unchanged,
        )
    )

    if plan.errors:
        print(f"errors: {len(plan.errors)} write(s) refused by the substrate")
        for error in plan.errors:
            print(f"  {error}")
    return 1 if plan.errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="write changes")
    mode.add_argument("--dry-run", action="store_true", help="print the plan; change nothing")
    args = parser.parse_args()

    requirements, conformances = load_registries()
    plan = reconcile(Substrate(), requirements, conformances, apply=args.apply)
    return report(plan)


if __name__ == "__main__":
    sys.exit(main())
