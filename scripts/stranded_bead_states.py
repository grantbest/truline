#!/usr/bin/env python3
"""Report beads whose state their own type's declared machine does not name.

Finding 1 of the stranded-incident spec: bead d861f65e-74ff-437e-a068-05e16b49c99a
was created 2026-08-06, before arch.incident's machine (R2605-1) existed, in
state 'pending' -- a state the machine has never declared. Both
`POST /beads/{id}/transition` and a state-changing `PATCH` compute
`machine.get(from_state, frozenset())`; when `from_state` is not a key of the
declared machine that is always the empty set, so the record was unreachable
by any governed write. `apps/substrate/migrations/versions/
0009_repair_stranded_incident_pending_state.py` is the reviewed, one-time
repair for that one named bead. This is the other half: the class, not just
the instance -- so a machine registered after a population can never again
strand a record silently without a check that would say so.

**This reports. It does not gate** -- the same contract `traceability.py`
states for itself: the exit code reflects whether the check could run, never
whether the population it found is clean. Turning a non-empty population into
a CI failure is a separate decision, deliberately not made here, and this
script is deliberately not wired into `check-repo-invariants.py`'s gating
`run_checks()` for the same reason `traceability.py` isn't.

A bead whose `(namespace, type)` carries no declared machine at all is never
reported -- `STATE_MACHINES.get((namespace, type))` returning `None` means
"permissive" everywhere in `routes.py`, not "violated", and this check must
read the same contract the store enforces.

Environment (only used by `--live`): SUBSTRATE_URL, SUBSTRATE_API_KEY.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

REPO = pathlib.Path(__file__).resolve().parent.parent

_SUBSTRATE_SRC = REPO / "apps" / "substrate" / "src"
if str(_SUBSTRATE_SRC) not in sys.path:
    sys.path.insert(0, str(_SUBSTRATE_SRC))
from bead_rules import STATE_MACHINES  # noqa: E402


@dataclass(frozen=True)
class StrandedBead:
    """One bead whose current state is not a key of its declared machine."""

    bead_id: str
    namespace: str
    type: str
    state: str
    declared_states: tuple[str, ...]


def find_stranded_states(
    beads: Iterable[Mapping[str, Any]],
    machines: Mapping[tuple[str, str], Mapping[str, Any]] = STATE_MACHINES,
) -> list[StrandedBead]:
    """Pure: no I/O, no substrate, no network. Fetching is the caller's problem.

    A bead is stranded when its `(namespace, type)` pair has a declared
    machine and its `state` is not one of that machine's keys -- the exact
    condition that makes `machine.get(state, frozenset())` always return the
    empty set, refusing every transition regardless of `to_state`.
    """
    stranded: list[StrandedBead] = []
    for bead in beads:
        namespace = bead.get("namespace")
        bead_type = bead.get("type")
        state = bead.get("state")
        machine = machines.get((namespace, bead_type))
        if machine is None:
            continue
        if state in machine:
            continue
        stranded.append(
            StrandedBead(
                bead_id=str(bead.get("id") or "(no id)"),
                namespace=str(namespace),
                type=str(bead_type),
                state=str(state),
                declared_states=tuple(sorted(machine.keys())),
            )
        )
    return stranded


def format_report(stranded: list[StrandedBead], beads_checked: int) -> str:
    lines = [f"Beads checked:    {beads_checked}", f"Stranded beads:   {len(stranded)}"]
    if stranded:
        lines.append("")
        lines.append("Beads in a state absent from their type's declared machine:")
        for item in stranded:
            lines.append(
                f"  - {item.bead_id} ({item.namespace}.{item.type}) "
                f"state={item.state!r} declared_states={list(item.declared_states)}"
            )
    return "\n".join(lines)


def _load_beads(path: pathlib.Path | None) -> list[dict]:
    """Records from a JSON file, or stdin when no path is given.

    Accepts a bare list, or a dict carrying the list under `beads`/`items` --
    the same shapes `arch-source-class-backfill.py`'s and `substrate_client.py`'s
    `list_beads` already return.
    """
    raw = path.read_text() if path else sys.stdin.read()
    data = json.loads(raw)
    if isinstance(data, dict):
        for key in ("beads", "items"):
            if isinstance(data.get(key), list):
                return data[key]
        return [data]
    return list(data)


def _fetch_live_beads(machines: Mapping[tuple[str, str], Any], client: Any) -> list[dict]:
    """Every bead of every `(namespace, type)` pair carrying a declared machine.

    `client` is expected to be `substrate_client.reader()`'s read-only facade
    (or a stand-in test double) -- an object with no `create`/`patch`/
    `add_link`/`delete` to call, so this check cannot itself mutate a bead by
    construction, not just by docstring. Injected rather than constructed here
    so the aggregation logic is testable with no network and no credentials.
    """
    beads: list[dict] = []
    for namespace, bead_type in sorted(machines):
        query = f"namespace={namespace}&type={bead_type}&limit=1000"
        beads.extend(client.get(f"/beads?{query}") or [])
    return beads


def _live_reader():
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import substrate_client  # noqa: E402

    return substrate_client.reader()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "beads", nargs="?", help="JSON file of bead records; stdin if omitted"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="fetch beads from SUBSTRATE_URL/SUBSTRATE_API_KEY instead of a file",
    )
    args = parser.parse_args(argv)

    try:
        if args.live:
            beads = _fetch_live_beads(STATE_MACHINES, _live_reader())
        else:
            beads = _load_beads(pathlib.Path(args.beads) if args.beads else None)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"stranded-bead-states: could not run: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # substrate_client.SubstrateError or a missing credential
        print(f"stranded-bead-states: could not run: {exc}", file=sys.stderr)
        return 2

    stranded = find_stranded_states(beads, STATE_MACHINES)
    print(format_report(stranded, len(beads)))
    # Deliberately 0 even when stranded beads were found. This reports a
    # population; it does not gate on one being clean -- see the module
    # docstring and traceability.py's identical contract.
    return 0


if __name__ == "__main__":
    sys.exit(main())
