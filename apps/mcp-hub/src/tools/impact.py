"""Impact analysis over the EA graph's typed edges (R26.09/O-4, PC-ASR-002/AC-1).

Answers "what does this change/application/CI affect" by walking the literal
chain the metamodel's edge vocabulary already carries
(``docs/architecture/bead-object-inventory.md#edge-vocabulary``):
``arch.change --affects--> arch.application``, ``arch.application
--depends_on--> arch.application`` (PROVIDER -> DEPENDENTS only -- the
edge's own CSDM verb is "Depends on :: Used by", and only the "used by"
direction is one a change propagates along; see ``.factory/design.md`` Sec 2
for why the previous both-direction closure over-reported).

What a reached application itself ``depends_on``/``consumes`` -- the CIs it
runs on, the services it calls -- is NOT part of ``affected`` (PR #1043 gate,
2026-09-25T20:30Z outer-loop answer, ``.factory/design.md`` Sec 1): impact
propagates from a provider to its dependents, never from a node to the
things it leans on. Those forward, one-hop edges are still read and still
returned -- as ``context.runs_on: {cis, services}`` -- but never counted
toward the blast radius. One served computation instead of the console's
client-side walk (``apps/lifeops-console/src/lib/blast-radius.ts``), so the
answer carries the coverage it was computed on -- see ``.factory/design.md``
for the full design record, including why an application with unknown
``depends_on`` posture is reported separately instead of being silently
folded into "affected" or "unaffected".

Posture comes from LINKS, not a content field (round-2 #1020 gate finding):
``content.depends_on`` predates #513's move of dependencies into typed
``depends_on`` links and is unpopulated for the live estate. This module
mirrors the rule ``apps/factory-dispatcher/activities/ea_dependency.py``'s
``land_dependency_posture``/``summarize_dependency_posture`` actually compute
(read-only reference -- this module never imports that tree; crossing that
boundary would need its own amendment): an application is "known" if it has
at least one outgoing ``depends_on`` edge to another ``arch.application``, or
an active assessed-none ``arch.observation`` (a fully-assessed application
confirmed to run nowhere). Everything else is "unknown", INCLUDING an
application whose only ``depends_on`` edges target CIs (ea_dependency's own
"coherence case": a real edge, just not an application-to-application one).

Posture decides whether the answer is COMPLETE, never whether real edges are
read: every reached application's ``depends_on``/``consumes`` edges are
walked into ``affected.cis``/``affected.services`` regardless of its own
posture, because a CI or service edge is what it is whether or not the
model has classified its owner's overall dependency posture yet (round-2
#1020 gate finding -- on the live graph, most applications are unknown by
this rule and would otherwise report an empty answer for real edges).
``affected.lower_bound`` is true whenever an unknown-posture application
contributed to the walk, naming that the true affected set may be larger
than what an unassessed application's edges have captured so far.

Reads through the one packaged substrate client (M12/OPS-190) via
``tools.finance._call_substrate`` -- this module never imports ``httpx`` and
never constructs the store's auth header itself; ``tests/test_impact.py``
asserts both directly against this module's source.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, Sequence, Set

ARCH_NAMESPACE = "arch"
KINDS = ("change", "application", "ci")
Kind = Literal["change", "application", "ci"]

PAGE_LIMIT = 200

AFFECTS = "affects"
DEPENDS_ON = "depends_on"
CONSUMES = "consumes"

# The bead types one impact computation reads. "observation" is read only to
# recognize an active assessed-none ``ea-dependency`` observation -- it never
# seeds or is expanded by the walk itself.
BEAD_TYPES = ("change", "application", "ci", "service", "observation")


class NotFound(RuntimeError):
    """No ``arch.<kind>`` bead exists with ``content.ref == ref``."""


class Unavailable(RuntimeError):
    """The population read needed to resolve this origin failed or was
    refused (PRIN-015): a collection read failing must never be reported as
    "no bead with that ref" -- that would assert a negative the read never
    actually established (round-2 #1020 gate finding)."""


ListBeads = Callable[[str, str, int], Awaitable[List[Dict[str, Any]]]]
ListLinks = Callable[[str], Awaitable[List[Dict[str, Any]]]]


@dataclass
class GraphPopulation:
    """Everything one impact computation reads, and how much of it actually
    landed -- the same shape ``coverage_observation`` (``scripts/ea-coverage.py``)
    keeps: the read is the measurement, not a separate pass over it."""

    changes: List[Dict[str, Any]] = field(default_factory=list)
    applications: List[Dict[str, Any]] = field(default_factory=list)
    cis: List[Dict[str, Any]] = field(default_factory=list)
    services: List[Dict[str, Any]] = field(default_factory=list)
    observations: List[Dict[str, Any]] = field(default_factory=list)
    links: List[Dict[str, Any]] = field(default_factory=list)
    partial: bool = False
    partial_reads: List[str] = field(default_factory=list)
    # Which of BEAD_TYPES' own collection reads failed outright -- distinct
    # from `partial_reads` (a human-readable log): this is what `get_impact`
    # checks to decide whether the ORIGIN's own kind could be resolved at
    # all, versus a degraded-but-answerable walk over the rest.
    failed_types: Set[str] = field(default_factory=set)


def _ref(bead: Dict[str, Any]) -> Optional[str]:
    return (bead.get("content") or {}).get("ref")


def _index_by_ref(beads: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {r: b for b in beads if (r := _ref(b))}


def _ref_part(value: str) -> str:
    """Mirrors ``ea_dependency.py``'s ``_ref_part`` exactly -- the assessed-
    none observation ref this module looks for is only findable if the two
    sides mangle a ref identically."""
    cleaned = "".join(
        ch.lower() if ch.isalnum() else "-"
        for ch in str(value).strip()
        if ch.isalnum() or ch in ".-_"
    ).strip("-")
    return cleaned or "unknown"


def _dependency_assessment_ref(app_ref: str) -> str:
    """Mirrors ``ea_dependency.py``'s ``dependency_assessment_ref`` -- the
    ``arch.observation`` ref an assessed-none finding for ``app_ref`` is
    filed under."""
    return f"obs.ea-dependency.{_ref_part(app_ref)}"


def _assessed_none_ref_set(observations: Sequence[Dict[str, Any]]) -> Set[str]:
    return {
        ref
        for o in observations
        if o.get("state") == "active" and (ref := (o.get("content") or {}).get("ref"))
    }


def _known_by_app_edge(app_id: str, dep_forward: Dict[str, set], app_by_id: Dict[str, Any]) -> bool:
    return any(target in app_by_id for target in dep_forward.get(app_id, set()))


def _depends_on_posture(
    app_id: str,
    app_ref: Optional[str],
    *,
    dep_forward: Dict[str, set],
    app_by_id: Dict[str, Any],
    assessed_none_obs_refs: Set[str],
) -> Literal["known", "unknown"]:
    """"known" (ea_dependency's ``known_by_edges``) requires an outgoing
    ``depends_on`` edge to another ``arch.application`` -- an application
    whose only ``depends_on`` edges target CIs is ea_dependency's own
    "coherence case" and stays unknown here too. The other "known" path is
    an active assessed-none observation: a fully-assessed application
    confirmed to depend on nothing. Everything else -- including no edges
    recorded at all -- is unknown: the model does not yet distinguish "no
    dependencies" from "not recorded" (R26.09/O-4)."""
    if _known_by_app_edge(app_id, dep_forward, app_by_id):
        return "known"
    if app_ref and _dependency_assessment_ref(app_ref) in assessed_none_obs_refs:
        return "known"
    return "unknown"


def _summarize(bead: Dict[str, Any]) -> Dict[str, Any]:
    content = bead.get("content") or {}
    return {"id": bead.get("id"), "ref": content.get("ref"), "name": content.get("name")}


def _sorted_summaries(beads: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [_summarize(b) for b in sorted(beads, key=lambda b: _ref(b) or "")]


def compute_impact(kind: str, ref: str, population: GraphPopulation) -> Dict[str, Any]:
    """Pure computation over an already-fetched :class:`GraphPopulation` --
    directly testable against the committed fixture, no substrate, no network.
    Raises :class:`NotFound` if no ``arch.<kind>`` bead has ``content.ref ==
    ref`` in the fetched population.
    """
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")

    by_ref = {
        "change": _index_by_ref(population.changes),
        "application": _index_by_ref(population.applications),
        "ci": _index_by_ref(population.cis),
    }
    origin = by_ref[kind].get(ref)
    if origin is None:
        raise NotFound(f"no arch.{kind} bead with content.ref={ref!r}")

    app_by_id = {a["id"]: a for a in population.applications}
    ci_by_id = {c["id"]: c for c in population.cis}
    service_by_id = {s["id"]: s for s in population.services}
    assessed_none_obs_refs = _assessed_none_ref_set(population.observations)

    dep_forward: Dict[str, set] = {}
    dep_reverse: Dict[str, set] = {}
    consumes_forward: Dict[str, set] = {}
    affects_forward: Dict[str, set] = {}
    for link in population.links:
        source, target, link_type = (
            link.get("source_id"),
            link.get("target_id"),
            link.get("link_type"),
        )
        if not source or not target or not link_type:
            continue
        if link_type == DEPENDS_ON:
            dep_forward.setdefault(source, set()).add(target)
            dep_reverse.setdefault(target, set()).add(source)
        elif link_type == CONSUMES:
            consumes_forward.setdefault(source, set()).add(target)
        elif link_type == AFFECTS:
            affects_forward.setdefault(source, set()).add(target)

    if kind == "change":
        seed_apps = {t for t in affects_forward.get(origin["id"], set()) if t in app_by_id}
    elif kind == "application":
        seed_apps = {origin["id"]}
    else:  # ci
        seed_apps = {s for s in dep_reverse.get(origin["id"], set()) if s in app_by_id}

    # Application <-> application depends_on closes in the PROVIDER -> its
    # DEPENDENTS direction only (.factory/design.md Sec 2): a change to an
    # affected app propagates to everything that depends on it (reverse
    # depends_on), transitively. It never walks forward into a dependency and
    # back out to that dependency's OTHER dependents -- two independent
    # consumers of a shared dependency do not affect each other. What a
    # visited app itself depends on (dep_forward) is never added to the
    # closure; its own CI/service edges are still surfaced below as direct,
    # one-hop context, never chained through another application. This
    # closure is already posture-independent -- an unknown-posture
    # application's app-to-app edges are walked exactly like a known one's.
    visited_apps: set = set()
    queue: List[str] = list(seed_apps)
    while queue:
        current = queue.pop()
        if current in visited_apps:
            continue
        visited_apps.add(current)
        for neighbor in dep_reverse.get(current, set()):
            if neighbor in app_by_id and neighbor not in visited_apps:
                queue.append(neighbor)

    known_apps: List[Dict[str, Any]] = []
    unknown_apps: List[Dict[str, Any]] = []
    for app_id in visited_apps:
        app = app_by_id[app_id]
        posture = _depends_on_posture(
            app_id,
            _ref(app),
            dep_forward=dep_forward,
            app_by_id=app_by_id,
            assessed_none_obs_refs=assessed_none_obs_refs,
        )
        (known_apps if posture == "known" else unknown_apps).append(app)

    # Every reached application's depends_on/consumes edges are walked into
    # CIs/services, whatever its own posture -- posture decides whether the
    # answer is COMPLETE, not whether a real edge gets read (round-2 #1020
    # gate finding: on the live graph most applications are unknown by this
    # rule, and gating this walk on "known" left every answer empty). This is
    # CONTEXT, not affected (PR #1043 gate, .factory/design.md Sec 1): what an
    # application depends on is not affected by a change to that application,
    # so these targets are reported under context.runs_on, never affected.
    context_ci_ids: set = set()
    context_service_ids: set = set()
    for app_id in visited_apps:
        context_ci_ids |= {t for t in dep_forward.get(app_id, set()) if t in ci_by_id}
        context_service_ids |= {
            t for t in consumes_forward.get(app_id, set()) if t in service_by_id
        }

    applications_assessed = sum(
        1
        for a in population.applications
        if _depends_on_posture(
            a["id"],
            _ref(a),
            dep_forward=dep_forward,
            app_by_id=app_by_id,
            assessed_none_obs_refs=assessed_none_obs_refs,
        )
        == "known"
    )
    cis_owned = sum(1 for c in population.cis if dep_reverse.get(c["id"]))
    changes_with_affects = sum(1 for c in population.changes if affects_forward.get(c["id"]))

    return {
        "affected": {
            "applications": _sorted_summaries(known_apps),
            # Always empty under the current edge vocabulary: no origin kind
            # (change/application/ci) ever directly targets a CI or service
            # the way an `affects` edge directly targets an application --
            # the only source that ever populated these was a reached
            # application's own forward depends_on/consumes edges, which is
            # now context.runs_on, not affected (.factory/design.md Sec 1).
            # Kept in the schema so a future edge type that targets a CI/
            # service directly (there is none today) can populate it without
            # a response-shape change.
            "cis": [],
            "services": [],
            "unknown_applications": _sorted_summaries(unknown_apps),
            # True whenever an unknown-posture application contributed to
            # the APPLICATION closure above: its own dependency posture is
            # not assessed, so the true set of affected applications may
            # extend further than what has been recorded for it so far. This
            # is about the application walk only -- context.runs_on carries
            # no completeness claim of its own (it is direct, one-hop,
            # never-chained context, not a walked closure).
            "lower_bound": bool(unknown_apps),
        },
        "context": {
            "runs_on": {
                "cis": _sorted_summaries([ci_by_id[i] for i in context_ci_ids]),
                "services": _sorted_summaries([service_by_id[i] for i in context_service_ids]),
            },
        },
        "coverage": {
            "applications_assessed": applications_assessed,
            "applications_total": len(population.applications),
            "cis_owned": cis_owned,
            "cis_total": len(population.cis),
            "changes_with_affects": changes_with_affects,
            "changes_total": len(population.changes),
            "partial": population.partial,
            "partial_reads": list(population.partial_reads),
            # Which BEAD_TYPES collection reads failed outright -- distinct
            # from partial_reads (a human-readable log line): this is what
            # the console checks to render a total as unknown ("--/--") for
            # exactly the types that failed, instead of the actually-empty-
            # because-failed collection reading as a real "0/0" (round-2
            # #1024 gate finding).
            "failed_types": sorted(population.failed_types),
        },
    }


async def _read_population(*, list_beads: ListBeads, list_links: ListLinks) -> GraphPopulation:
    """Fetch the full population every impact computation walks, plus the
    links of every bead the walk can actually reach through -- changes,
    applications, CIs, services -- the same shape ``Architecture.tsx`` already
    validated at production scale (there is no bulk edge-listing route; see
    ``.factory/design.md`` Sec 4). ``arch.observation`` beads are still listed
    above for their own state/content.ref, but never included in this link
    fan-out: the walk never follows an edge touching one, so reading their
    links is pure waste, and on the live graph the fastest-growing one (see
    ``.factory/design.md`` Sec 4). A failed or unexhausted page degrades its
    own collection to empty rather than raising (PRIN-015): the failure is
    named in ``partial_reads`` and recorded in ``failed_types``, never a
    silently-fabricated zero that reads as a real measurement.
    """
    partial_reads: List[str] = []
    failed_types: Set[str] = set()

    async def _safe_list(type_: str) -> List[Dict[str, Any]]:
        try:
            return await list_beads(ARCH_NAMESPACE, type_, PAGE_LIMIT)
        except Exception as exc:  # noqa: BLE001 - degrade this one read, not the whole response
            partial_reads.append(f"arch.{type_}: {exc}")
            failed_types.add(type_)
            return []

    changes, applications, cis, services, observations = await asyncio.gather(
        *(_safe_list(type_) for type_ in BEAD_TYPES)
    )

    # Observation beads are read for state/content.ref only (assessed-none
    # posture) -- the walk never follows an edge touching one, so their links
    # are never requested. On the live graph this is the fastest-growing part
    # of the read (nightly ea-observation runs); skipping it dropped the
    # measured read from ~1,949 list_links calls / ~30s to ~388 calls / ~7s
    # (round-2 #1024 gate finding, .factory/design.md Sec 4).
    link_targets = [*changes, *applications, *cis, *services]

    async def _safe_links(bead_id: str) -> List[Dict[str, Any]]:
        try:
            return await list_links(bead_id)
        except Exception as exc:  # noqa: BLE001 - same degrade-this-read contract
            partial_reads.append(f"links({bead_id}): {exc}")
            return []

    link_groups = await asyncio.gather(*(_safe_links(b["id"]) for b in link_targets))
    links_by_id: Dict[str, Dict[str, Any]] = {}
    for group in link_groups:
        for link in group:
            link_id = link.get("id")
            if link_id is not None:
                links_by_id[link_id] = link

    return GraphPopulation(
        changes=changes,
        applications=applications,
        cis=cis,
        services=services,
        observations=observations,
        links=list(links_by_id.values()),
        partial=bool(partial_reads),
        partial_reads=partial_reads,
        failed_types=failed_types,
    )


async def _default_list_beads(namespace: str, type_: str, limit: int) -> List[Dict[str, Any]]:
    from .finance import _call_substrate

    return await _call_substrate("list_beads", namespace, type_, limit=limit)


async def _default_list_links(bead_id: str) -> List[Dict[str, Any]]:
    from .finance import _call_substrate

    return await _call_substrate("list_links", bead_id)


async def get_impact(
    kind: str,
    ref: str,
    *,
    list_beads: Optional[ListBeads] = None,
    list_links: Optional[ListLinks] = None,
) -> Dict[str, Any]:
    """The route's own entry point -- fetches the population through the
    packaged substrate client (M12) and computes the answer over it.

    ``list_beads``/``list_links`` are injectable for tests; production
    callers leave them at their real, substrate-backed defaults.
    """
    population = await _read_population(
        list_beads=list_beads or _default_list_beads,
        list_links=list_links or _default_list_links,
    )
    if kind in population.failed_types:
        # The origin's OWN collection read failed or was refused -- there is
        # no basis to say `ref` doesn't exist (that would be a 404 asserting
        # a negative the read never established). 503, never NotFound
        # (round-2 #1020 gate finding).
        reasons = [r for r in population.partial_reads if r.startswith(f"arch.{kind}:")]
        raise Unavailable(
            f"arch.{kind} population read failed; cannot determine whether "
            f"{kind}={ref!r} exists ({'; '.join(reasons) or 'no reason recorded'})"
        )
    result = compute_impact(kind, ref, population)
    result["computed_at"] = datetime.now(timezone.utc).isoformat()
    result["revision_hint"] = os.environ.get("GIT_SHA", "unknown")
    return result
