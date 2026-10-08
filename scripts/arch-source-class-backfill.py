#!/usr/bin/env python3
"""Classify existing ``arch.*`` beads that predate ``content.source_class``.

Every ``arch.*`` bead now carries ``content.source_class`` in
``{authored, derived, observed}`` — the substrate's write path already reads a
bead missing the field as ``authored`` (never invalid), so this backfill does not
change what any write is allowed to do. It only makes that implicit
classification explicit content on the bead itself, one time, for beads written
before the field existed.

* Beads that already carry ``content.source_class`` (of any value) are left
  alone — this is not a relabeling tool.
* Dry-run is the default. Writes require ``--apply``.
* No delete path exists in this script.
* Idempotent: a second ``--apply`` run finds nothing left to classify.

Environment: SUBSTRATE_URL, SUBSTRATE_API_KEY.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from substrate_client import Substrate as _Substrate  # noqa: E402
from substrate_client import SubstrateClient, SubstrateError  # noqa: E402

# ea-metamodel.md §2 Stage 1/2 object types, plus the closed Track B
# observation type — matches ARCH_TYPE_SCHEMAS in apps/substrate/src/schemas.py.
ARCH_TYPES = (
    "capability",
    "application",
    "service",
    "information_object",
    "requirement",
    "principle",
    "observation",
    "change",
)
CREATED_BY = "arch-source-class-backfill"
DEFAULT_SOURCE_CLASS = "authored"


class Substrate(_Substrate):
    """scripts/substrate_client.py's shared client, fixed to this script's
    writer identity on its default "arch" namespace -- the same pattern
    ea-load.py/release-load.py/requirements-load.py already use, rather than
    a fifth hand-rolled X-API-Key client."""

    def __init__(self) -> None:
        super().__init__(created_by=CREATED_BY)

    # This script's module docstring promises "No delete path exists in this
    # script", and test_backfill_never_deletes is the guard for that promise.
    # Subclassing the shared client inherited delete_bead and delete_link,
    # which made the promise false while the guard -- which looked for the
    # literal name "delete" -- went on passing. Nothing here calls them, so no
    # executed path changes; these overrides exist so the promise is enforced
    # by the type rather than by nobody having written the call yet.
    def delete_bead(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError(
            "arch-source-class-backfill.py has no delete path: it classifies "
            "source_class on existing beads and never removes one"
        )

    def delete_link(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError(
            "arch-source-class-backfill.py has no delete path: it classifies "
            "source_class on existing beads and never removes a link"
        )


class Plan:
    def __init__(self, apply: bool):
        self.apply = apply
        self.classified: list[str] = []
        self.already_classified: list[str] = []
        self.errors: list[str] = []

    @property
    def dry_run(self) -> bool:
        return not self.apply


def _bead_label(bead_type: str, bead: dict) -> str:
    content = bead.get("content") or {}
    ref = content.get("ref") or content.get("id")
    return f"{bead_type}:{ref or bead.get('id') or '?'}"


def reconcile(sub: SubstrateClient, apply: bool = False) -> Plan:
    plan = Plan(apply=apply)
    for bead_type in ARCH_TYPES:
        for bead in sub.list_beads(bead_type):
            content = bead.get("content") or {}
            label = _bead_label(bead_type, bead)
            if "source_class" in content:
                plan.already_classified.append(label)
                continue

            plan.classified.append(label)
            if apply:
                try:
                    sub.patch(
                        bead["id"],
                        {"content": {**content, "source_class": DEFAULT_SOURCE_CLASS}},
                    )
                except SubstrateError as exc:
                    plan.errors.append(
                        f"{label}: backfill rejected: substrate {exc.status} on {exc.path}"
                    )
    return plan


def report(plan: Plan) -> int:
    head = "DRY RUN - nothing was written" if plan.dry_run else "applied"
    print(f"arch-source-class-backfill: {head}")
    print(
        "beads: "
        f"classified={len(plan.classified)} "
        f"already_classified={len(plan.already_classified)}"
    )
    if plan.errors:
        print("errors:")
        for error in plan.errors:
            print(f"  {error}")
    return 1 if plan.errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="write changes")
    mode.add_argument("--dry-run", action="store_true", help="print the plan; change nothing")
    args = parser.parse_args()

    plan = reconcile(Substrate(), apply=args.apply)
    return report(plan)


if __name__ == "__main__":
    sys.exit(main())
