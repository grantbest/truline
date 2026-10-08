#!/usr/bin/env python3
"""Report arch.change beads that carry no `affects` edge to an arch.application.

Finding 3 of the stranded-incident spec: 19 of 65 `arch.change` beads (as
measured live 2026-09-10) carry no `affects` edge at all. The namespace is
defined as "the service model and the ITSM records that move it"
(bead-object-inventory.md); a change record that reaches no application moves
nothing.

This is a report, not a verdict: whether an unattached change is a defect or
a legitimate state (a change scoped to a capability rather than an
application, say) is a product-owner call this script does not make -- it
makes the population countable by the platform instead of assembled by hand,
the same posture `check_requirement_citations_reported` took on a different
population before #692 made that call.

**Direction, stated explicitly.** Finding 2 of the same spec: `GET
/beads/{id}/links` defaults to `direction=both`, which double-counts any edge
whose both endpoints share a namespace when a caller traverses per-bead and
sums. The live-fetch path here asks for `direction=outgoing` and
`link_type=affects` by name on every request -- never the default -- so this
script cannot reproduce that miscount.

**This reports. It does not gate**, matching `stranded_bead_states.py` and
`traceability.py`: the exit code reflects whether the check could run, never
whether the population is clean.

Environment (only used by `--live`): SUBSTRATE_URL, SUBSTRATE_API_KEY.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any, Iterable, Mapping

REPO = pathlib.Path(__file__).resolve().parent.parent
LINK_TYPE = "affects"
TARGET_TYPE = "application"


def find_unattached_changes(
    changes: Iterable[Mapping[str, Any]],
    links: Iterable[Mapping[str, Any]],
    applications: Iterable[Any],
) -> list[str]:
    """Pure: no I/O, no substrate, no network. Fetching is the caller's problem.

    `links` is every `affects` edge already known to originate from one of
    `changes` (the caller decides how it gathered them -- per-bead traversal,
    a bulk link dump, whatever). `applications` is the set of arch.application
    bead ids. A change is "unattached" when none of its `affects` edges lands
    on an id in `applications`.
    """
    application_ids = {str(a) for a in applications}
    attached_change_ids = {
        str(link["source_id"])
        for link in links
        if str(link.get("link_type")) == LINK_TYPE
        and str(link.get("target_id")) in application_ids
    }
    return [
        str(change["id"])
        for change in changes
        if str(change["id"]) not in attached_change_ids
    ]


def format_report(unattached: list[str], changes_checked: int) -> str:
    lines = [
        f"arch.change beads checked: {changes_checked}",
        f"Reaching no arch.application via '{LINK_TYPE}': {len(unattached)}",
    ]
    if unattached:
        lines.append("")
        lines.append("Unattached changes:")
        lines.extend(f"  - {change_id}" for change_id in unattached)
    return "\n".join(lines)


def _load_snapshot(path: pathlib.Path | None) -> dict:
    """A JSON file or stdin carrying `{"changes": [...], "links": [...],
    "applications": [...]}`. Fetching is deliberately not this function's job --
    the same separation `traceability.py`'s `_load_tasks` keeps."""
    raw = path.read_text() if path else sys.stdin.read()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object with changes/links/applications")
    return data


def _fetch_live_snapshot(client: Any) -> dict:
    """Every arch.change bead, every arch.application id, and every outgoing
    `affects` edge from each change -- fetched with `direction=outgoing`
    stated explicitly on every request, never the endpoint's `both` default.

    `client` is `substrate_client.reader()`'s read-only facade (or a test
    double) -- injected rather than constructed here so this aggregation is
    testable with no network and no credentials.
    """
    changes = client.get("/beads?namespace=arch&type=change&limit=1000") or []
    applications = client.get("/beads?namespace=arch&type=application&limit=1000") or []

    links: list[dict] = []
    for change in changes:
        change_id = change.get("id")
        if not change_id:
            continue
        links.extend(
            client.get(
                f"/beads/{change_id}/links?direction=outgoing&link_type={LINK_TYPE}"
            )
            or []
        )

    return {
        "changes": changes,
        "links": links,
        "applications": [app.get("id") for app in applications],
    }


def _live_reader():
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import substrate_client  # noqa: E402

    return substrate_client.reader()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "snapshot",
        nargs="?",
        help="JSON file with changes/links/applications; stdin if omitted",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="fetch from SUBSTRATE_URL/SUBSTRATE_API_KEY instead of a file",
    )
    args = parser.parse_args(argv)

    try:
        if args.live:
            snapshot = _fetch_live_snapshot(_live_reader())
        else:
            snapshot = _load_snapshot(
                pathlib.Path(args.snapshot) if args.snapshot else None
            )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"unattached-arch-changes: could not run: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # substrate_client.SubstrateError or a missing credential
        print(f"unattached-arch-changes: could not run: {exc}", file=sys.stderr)
        return 2

    changes = snapshot.get("changes") or []
    unattached = find_unattached_changes(
        changes, snapshot.get("links") or [], snapshot.get("applications") or []
    )
    print(format_report(unattached, len(changes)))
    # Deliberately 0 even when unattached changes were found -- see the module
    # docstring. Whether 19-of-65 is a defect is a product-owner call this
    # script does not make.
    return 0


if __name__ == "__main__":
    sys.exit(main())
