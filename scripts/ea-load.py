#!/usr/bin/env python3
"""Reconcile the EA model from git into the substrate.

`docs/architecture/model/*.yaml` is authoritative for **structure**; the
substrate is authoritative for **lifecycle** (ea-metamodel.md §6). This script
is the only thing that crosses that line, so its rules are worth stating before
its code:

**Idempotent on `content.ref`.** Running it twice equals running it once. The
ref is the stable business key — objects are matched on it, never on UUID, so
the model survives a substrate rebuild and stays diffable in git.

**Structure is overwritten. State is not.** A capability's name, description,
maturity, evidence and edges come from git every run. Its `bead.state` is set
once at creation and then left alone, because a capability going `active`
because something finally realises it is a lifecycle event, not a text edit.
When git and the substrate disagree about state the divergence is *reported*,
never silently resolved — `--adopt-state` forces git's value when you mean to
re-seed.

That asymmetry is the whole point of §6. Without it, every `ea-load` run would
quietly revert lifecycle transitions back to whatever the YAML happened to say
when it was last edited.

**Edges are reconciled, not appended.** `supports`, `realizes` and `depends_on`
become `bead_link` rows that exactly match the YAML: missing edges are created,
extra ones are deleted. An edge removed from git is removed from the graph.

**Deletions are reported, not performed.** An `arch` bead whose ref is no longer
in the model is listed and left alone unless `--prune` is passed. Retiring an
application is a reviewed decision (see EA-S4); a loader that deletes on a
missing line would make it an accident.

Usage:
    python3 scripts/ea-load.py --dry-run     # print the plan, change nothing
    python3 scripts/ea-load.py               # apply
    python3 scripts/ea-load.py --prune       # also delete orphaned arch beads
    python3 scripts/ea-load.py --check       # read-only; exit nonzero if the model
                                              # and the substrate diverge (PC-ASR-007/AC-4)

Environment: SUBSTRATE_URL, SUBSTRATE_API_KEY.
"""

from __future__ import annotations

import argparse
import importlib.util
import pathlib
import sys
from typing import Any, Iterable

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("pyyaml is required: pip install pyyaml")

REPO = pathlib.Path(__file__).resolve().parent.parent
MODEL_DIR = REPO / "docs" / "architecture" / "model"
CREATED_BY = "ea-load"

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
SubstrateError = _substrate_client_module.SubstrateError  # noqa: F401 -- re-exported for callers

# (yaml file, key, bead type, required). Capabilities load first: an
# application's `realizes` edges point at them, and an edge cannot be written
# before its target bead exists. services.yaml is optional like
# observations.yaml — a fixture model in a test need not carry one.
SOURCES = (
    ("business-layer.yaml", "capabilities", "capability", True),
    ("application-portfolio.yaml", "applications", "application", True),
    ("services.yaml", "services", "service", False),
    ("observations.yaml", "observations", "observation", False),
)

# Edge name -> (source types, target type). The loader writes these as
# bead_link rows; the YAML authors them as ref arrays because that is what
# reviews well in a PR (ea-metamodel.md §5.3). `depends_on` originates from
# both applications and services (a service's depends_on always targets an
# application, e.g. svc.foundation -> app.substrate/app.temporal); `consumes`
# originates only from applications, targeting a service.
EDGES = {
    "supports": (("capability",), "capability"),
    "realizes": (("application",), "capability"),
    "depends_on": (("application", "service"), "application"),
    "consumes": (("application",), "service"),
    "measures": (("observation",), "application"),
}
BEAD_TYPES = tuple(dict.fromkeys(source[2] for source in SOURCES))

# `list_beads` answers one page. A bead type whose population outgrows the
# page size (arch.observation: nightly per-app mints, newest-first, past
# 1,000 rows as of 2026-09-19) hides everything older than the page from an
# index built off a single call -- a declared-and-present ref then reads as
# absent, gets re-POSTed, and the store's own idx_unique_arch_ref refuses the
# duplicate. Same class of defect as OPS-181 (status_bead.find_status scanning
# a capped list); here the fix is a full walk rather than a single indexed
# lookup, because reconcile_objects' orphan/prune pass needs the entire
# population regardless of how create-vs-update is decided.
_PAGE_SIZE = 1000
_MAX_PAGES = 500  # 500,000 rows per bead type -- loud past this, never a silent truncation


def _list_all(sub: Substrate, bead_type: str) -> list[dict]:
    """Every bead of `bead_type`, walking every page by `offset` until one
    comes back shorter than `_PAGE_SIZE` -- the same algorithm the packaged
    client's `list_beads` uses (apps/substrate/client/src/substrate_client/client.py),
    inlined here rather than pushed into `Substrate.list_beads` itself so the
    walk is visible to (and exercised by) an in-process double whose
    `list_beads` only ever answers one page, the way the real store's does.
    """
    results: list[dict] = []
    offset = 0
    for _ in range(_MAX_PAGES):
        page = sub.list_beads(bead_type, limit=_PAGE_SIZE, offset=offset)
        results.extend(page)
        if len(page) < _PAGE_SIZE:
            return results
        offset += _PAGE_SIZE
    sys.exit(
        f"paging arch.{bead_type} did not exhaust after {_MAX_PAGES} pages of "
        f"{_PAGE_SIZE} ({len(results)} rows so far). Either the population is "
        "larger than this walk is designed for, or the store is not honouring "
        "offset. Refusing to reconcile against a population that may be "
        "silently incomplete."
    )


class Substrate(_Substrate):
    """Only the calls this loader needs, and no retry logic.

    A partial load is safe to re-run — that is what idempotency buys — so a
    failure should stop loudly rather than be papered over mid-reconcile.
    """

    def __init__(self) -> None:
        super().__init__(created_by=CREATED_BY)


class Plan:
    """What the run did, or would do. Printed whole so a dry run is reviewable."""

    def __init__(self, dry_run: bool) -> None:
        self.dry_run = dry_run
        self.created: list[str] = []
        self.updated: list[str] = []
        self.unchanged: list[str] = []
        self.links_added: list[str] = []
        self.links_removed: list[str] = []
        self.links_foreign: list[str] = []
        self.state_divergence: list[str] = []
        self.orphans: list[str] = []
        self.pruned: list[str] = []


def _jsonable(value: Any) -> Any:
    """YAML gives real `date` objects; JSON does not take them."""
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def load_model(model_dir: pathlib.Path = MODEL_DIR) -> tuple[dict[str, dict], dict[str, str]]:
    """Return {ref: object} and {ref: bead_type} across both model files."""
    objects: dict[str, dict] = {}
    types: dict[str, str] = {}
    for filename, key, bead_type, required in SOURCES:
        path = model_dir / filename
        if not path.exists():
            if required:
                sys.exit(f"model file missing: {path}")
            continue
        for obj in yaml.safe_load(path.read_text())[key]:
            ref = obj["ref"]
            if ref in objects:
                sys.exit(f"duplicate ref across model files: {ref}")
            objects[ref] = obj
            types[ref] = bead_type
    return objects, types


def content_for(obj: dict) -> dict:
    """The bead content: the YAML `content` block plus the ref itself.

    `ref` lives at the object level in YAML because that is what reads well in
    a diff, but the substrate's arch schemas require it inside `content` — it
    is the business key, and a key outside the payload cannot be indexed.
    """
    content = _jsonable(dict(obj["content"]))
    content["ref"] = obj["ref"]
    # Authored as ref arrays for review; the graph lives in bead_link. Keeping
    # both would give two answers to "what does this depend on", and the copy
    # in content is the one nothing enforces.
    for edge in EDGES:
        content.pop(edge, None)
    return content


def order_capabilities(objects: dict[str, dict], types: dict[str, str]) -> list[str]:
    """Parents before children, so `parent_id` always resolves on first write."""
    caps = [r for r, t in types.items() if t == "capability"]
    ordered: list[str] = []
    seen: set[str] = set()

    def visit(ref: str, stack: tuple[str, ...] = ()) -> None:
        if ref in seen:
            return
        if ref in stack:
            sys.exit(f"capability parent cycle: {' -> '.join(stack + (ref,))}")
        parent = objects[ref].get("parent")
        if parent:
            if parent not in objects:
                sys.exit(f"{ref}: parent {parent} is not in the model")
            visit(parent, stack + (ref,))
        seen.add(ref)
        ordered.append(ref)

    for ref in caps:
        visit(ref)
    return ordered


def reconcile_objects(sub: Substrate, objects, types, order, plan, adopt_state) -> dict[str, str]:
    """Create or update every modelled object. Returns {ref: bead_id}."""
    existing: dict[str, dict] = {}
    for bead_type in BEAD_TYPES:
        for bead in _list_all(sub, bead_type):
            ref = (bead.get("content") or {}).get("ref")
            if ref:
                existing[ref] = bead

    ids: dict[str, str] = {r: b["id"] for r, b in existing.items()}

    for ref in order:
        obj = objects[ref]
        bead_type = types[ref]
        content = content_for(obj)
        yaml_state = obj["state"]
        parent_ref = obj.get("parent")
        parent_id = ids.get(parent_ref) if parent_ref else None
        if parent_ref and not parent_id:
            sys.exit(f"{ref}: parent {parent_ref} has no bead — ordering bug")

        current = existing.get(ref)
        if current is None:
            plan.created.append(f"{bead_type} {ref} (state={yaml_state})")
            if not plan.dry_run:
                bead = sub.create(bead_type, yaml_state, content, parent_id)
                ids[ref] = bead["id"]
            else:
                ids[ref] = f"dry-run:{ref}"
            continue

        # Structure is git's. State is the substrate's unless told otherwise.
        if current.get("state") != yaml_state:
            plan.state_divergence.append(
                f"{ref}: substrate={current.get('state')} git={yaml_state}"
                + (" — adopting git" if adopt_state else " — left alone")
            )

        body: dict[str, Any] = {}
        if current.get("content") != content:
            body["content"] = content
        if str(current.get("parent_id") or "") != str(parent_id or ""):
            body["parent_id"] = parent_id
        if adopt_state and current.get("state") != yaml_state:
            body["state"] = yaml_state

        if body:
            plan.updated.append(f"{bead_type} {ref} ({', '.join(sorted(body))})")
            if not plan.dry_run:
                sub.patch(current["id"], body)
        else:
            plan.unchanged.append(ref)

    for ref, bead in existing.items():
        if ref not in objects:
            plan.orphans.append(f"{bead['type']} {ref} ({bead['id']})")
    return ids


def _written_by_ea_load(link: dict) -> bool:
    """True when this outgoing link's recorded writer is ea-load itself.

    dev.finding 3746f1f0: two other writers (the EA observer and ea-derive)
    share the `depends_on` link type on `arch.application` beads. A link is
    ea-load's to delete only when its `created_by` says so by evidence --
    never on a guess, so a missing key or a literal 'unknown' both read as
    foreign here, not as ea-load's.
    """
    return link.get("created_by") == CREATED_BY


def reconcile_edges(sub: Substrate, objects, types, ids, plan) -> None:
    """Make bead_link exactly match the YAML's ref arrays -- for the links
    ea-load itself owns.

    dev.finding 3746f1f0: since 2026-09-13 other writers (the EA observer,
    ea-derive) hold their own links of the same EDGES types on the same
    beads. Two rules follow:

    * **Deletion is ownership-scoped.** Only a link whose `created_by`
      equals `CREATED_BY` (`_written_by_ea_load`) is ever eligible for
      `delete_link`. A link belonging to any other writer -- including one
      with `created_by` missing or `'unknown'` -- is left alone and
      reported in `plan.links_foreign` instead, never deleted on a guess.
    * **Addition is ownership-blind.** The YAML-vs-existing comparison that
      decides what to *add* treats a link written by any writer as already
      present, so ea-load never tries to re-add an edge another writer
      already holds (which `bead_link`'s
      `UNIQUE(source_id, target_id, link_type)` would refuse anyway).
    """
    for ref, obj in objects.items():
        source_id = ids.get(ref)
        if not source_id or source_id.startswith("dry-run:"):
            if plan.dry_run:
                for edge, (src_types, _) in EDGES.items():
                    if types[ref] in src_types:
                        for target in obj["content"].get(edge, []):
                            plan.links_added.append(f"{ref} -{edge}-> {target}")
            continue

        wanted: set[tuple[str, str]] = set()
        for edge, (src_types, _) in EDGES.items():
            if types[ref] not in src_types:
                continue
            for target in obj["content"].get(edge, []):
                if target not in ids:
                    sys.exit(f"{ref}: {edge} target {target} is not in the model")
                wanted.add((edge, ids[target]))

        have: dict[tuple[str, str], str] = {}
        present: set[tuple[str, str]] = set()
        foreign: list[tuple[tuple[str, str], str]] = []
        for link in sub.links(source_id, "outgoing"):
            key = (link["link_type"], str(link["target_id"]))
            if key[0] not in EDGES:
                continue
            present.add(key)
            if _written_by_ea_load(link):
                have[key] = link["id"]
            else:
                foreign.append((key, link.get("created_by", "<missing>")))

        for edge, target_id in sorted(wanted - present):
            plan.links_added.append(f"{ref} -{edge}-> {target_id}")
            if not plan.dry_run:
                sub.add_link(source_id, target_id, edge)

        for key in sorted(set(have) - wanted):
            plan.links_removed.append(f"{ref} -{key[0]}-> {key[1]}")
            if not plan.dry_run:
                sub.delete_link(have[key])

        for key, writer in sorted(foreign):
            if key in wanted:
                continue
            plan.links_foreign.append(f"{ref} -{key[0]}-> {key[1]} [created_by={writer}]")


def build_plan(
    sub: Substrate,
    *,
    dry_run: bool,
    adopt_state: bool = False,
    model_dir: pathlib.Path = MODEL_DIR,
) -> Plan:
    """Reconcile the model at `model_dir` against `sub` and return the resulting plan.

    The reusable core of a reconcile run — CLI apply, CLI --dry-run/--check, and the in-cluster
    applier (apps/factory-dispatcher/activities/ea_apply.py) all call this rather than each
    re-deriving object order and calling reconcile_objects/reconcile_edges themselves.
    """
    objects, types = load_model(model_dir)
    order = (
        order_capabilities(objects, types)
        + [r for r, t in types.items() if t == "application"]
        + [r for r, t in types.items() if t not in {"capability", "application"}]
    )
    if len(order) != len(objects):
        sys.exit("ordering dropped objects — refusing to load a partial model")

    plan = Plan(dry_run)
    ids = reconcile_objects(sub, objects, types, order, plan, adopt_state)
    reconcile_edges(sub, objects, types, ids, plan)
    return plan


def plan_diverged(plan: Plan) -> bool:
    """True when the model and the substrate disagree about anything --check cares about."""
    return bool(
        plan.created
        or plan.updated
        or plan.links_added
        or plan.links_removed
        or plan.state_divergence
        or plan.orphans
    )


def report(plan: Plan) -> int:
    head = "DRY RUN — nothing was written" if plan.dry_run else "applied"
    print(f"ea-load: {head}\n")

    def section(title: str, items: Iterable[str], show: int = 40) -> None:
        items = list(items)
        if not items:
            return
        print(f"{title} ({len(items)})")
        for line in items[:show]:
            print(f"  {line}")
        if len(items) > show:
            print(f"  … and {len(items) - show} more")
        print()

    section("created", plan.created)
    section("updated", plan.updated)
    section("links added", plan.links_added)
    section("links removed", plan.links_removed)
    section("foreign links left alone", plan.links_foreign)
    section("state divergence — git and the substrate disagree", plan.state_divergence)
    section("orphans — in the substrate, not in the model", plan.orphans)
    section("pruned", plan.pruned)

    print(f"unchanged: {len(plan.unchanged)}")
    if plan.orphans and not plan.pruned:
        print("\nOrphans were left alone. Re-run with --prune to delete them,")
        print("but retiring an object is a reviewed decision — check the model first.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dry-run", action="store_true", help="print the plan; change nothing")
    ap.add_argument("--adopt-state", action="store_true",
                    help="let git's state win over the substrate's (re-seeding)")
    ap.add_argument("--prune", action="store_true",
                    help="delete arch beads whose ref is no longer in the model")
    ap.add_argument("--check", action="store_true",
                    help="read-only: print the plan, exit nonzero if the model and the "
                         "substrate diverge, zero if reconciled; never writes")
    args = ap.parse_args()

    if args.check and (args.prune or args.adopt_state):
        sys.exit("--check is read-only; it cannot be combined with --prune or --adopt-state")

    dry_run = args.dry_run or args.check
    sub = Substrate()
    plan = build_plan(sub, dry_run=dry_run, adopt_state=args.adopt_state)

    if args.prune and plan.orphans and not dry_run:
        for line in plan.orphans:
            bead_id = line.rsplit("(", 1)[1].rstrip(")")
            sub.delete_bead(bead_id)
            plan.pruned.append(line)

    report(plan)
    if args.check:
        return 1 if plan_diverged(plan) else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
