#!/usr/bin/env python3
"""File a dev.finding bead from a JSON spec.

    python file_finding.py spec.json --prompt-ref pr#1234
    cat spec.json | FACTORY_OPERATOR=grant python file_finding.py - --prompt-ref audit-2026-10-01

The spec is a JSON object with:

    {
      "kind": "bug",
      "disposition": "blocking",
      "severity": "high",
      "summary": "...",
      "evidence": "...",
      "state": "backlogged"
    }

``kind``/``disposition``/``severity``/the blocking-needs-evidence rule mirror
``apps/substrate/src/schemas.py``'s ``DevFindingContent`` -- the store's own
content model for ``dev.finding`` -- so a spec this module accepts is one the
store accepts too. Any further keys ride into content unchanged (the store's
content model allows extras).

``state`` is NOT one of those content fields: it is the disposition this
filer transitions the finding to immediately after creating it, and it has NO
DEFAULT. A finding is born pending only -- ``STATE_MACHINE_ENTRY_STATES``
(apps/substrate/src/bead_rules.py) declares exactly one legal entry state for
``("dev", "finding")`` -- and the machine's other four states
(backlogged/ruled/already_fixed/not_a_defect) are each terminal, with no
outbound edge. A spec with no ``state``, with ``pending``, or with a
misspelt value is therefore refused before any write: CLAUDE.md's
2026-09-11 rule is that every operational observation becomes a dev.finding
DISPOSITIONED BEFORE the dev.task it produces, and a finding filed with no
disposition state is exactly the shape that rule exists to prevent. ``state``
is consumed here and never written into ``content`` -- the store would
accept it silently under the content model's ``extra="allow"``, which would
make the omission invisible rather than refused.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from typing import Any

from substrate import Substrate

# Mirrors apps/substrate/src/schemas.py DevFindingContent.
FINDING_KINDS = frozenset({"bug", "enhancement", "security"})
FINDING_DISPOSITIONS = frozenset({"blocking", "backlog"})
FINDING_SEVERITIES = frozenset({"high", "medium", "low"})

# Mirrors apps/substrate/src/bead_rules.py STATE_MACHINES[("dev", "finding")]'s
# four outbound edges from "pending" -- the only state a finding is ever
# dispositioned into, and each one terminal.
FINDING_DISPOSITION_STATES = ("backlogged", "ruled", "already_fixed", "not_a_defect")


def _validate_and_build_content(spec: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Validate ``spec`` the way the store's ``dev.finding`` schema will, and
    return ``(content, disposition_state)``. Raises ``SystemExit`` naming the
    offending field on the first problem found. No store call happens here."""
    if not isinstance(spec, dict):
        raise SystemExit(f"spec must be a JSON object; got {type(spec).__name__}")

    kind = spec.get("kind")
    if kind not in FINDING_KINDS:
        raise SystemExit(
            f"spec field 'kind' is {kind!r}; must be one of "
            f"{', '.join(sorted(FINDING_KINDS))}."
        )

    disposition = spec.get("disposition")
    if disposition not in FINDING_DISPOSITIONS:
        raise SystemExit(
            f"spec field 'disposition' is {disposition!r}; must be one of "
            f"{', '.join(sorted(FINDING_DISPOSITIONS))}."
        )

    severity = spec.get("severity")
    if severity not in FINDING_SEVERITIES:
        raise SystemExit(
            f"spec field 'severity' is {severity!r}; must be one of "
            f"{', '.join(sorted(FINDING_SEVERITIES))}."
        )

    summary = spec.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise SystemExit("spec field 'summary' must be a non-empty string.")

    for field in ("evidence", "reproduction"):
        value = spec.get(field)
        if value is not None and not isinstance(value, str):
            raise SystemExit(f"spec field {field!r} must be a string.")

    evidence = spec.get("evidence")
    if disposition == "blocking" and not (isinstance(evidence, str) and evidence.strip()):
        raise SystemExit(
            "spec field 'evidence' is required and must be non-blank when "
            "disposition is 'blocking' (DevFindingContent."
            "blocking_findings_must_carry_evidence)."
        )

    state = spec.get("state")
    if state not in FINDING_DISPOSITION_STATES:
        raise SystemExit(
            f"spec field 'state' is {state!r}; must be one of the four "
            f"admitted disposition states: {', '.join(FINDING_DISPOSITION_STATES)}.\n"
            "A finding is born pending only; pending is not a disposition, "
            "and a finding dispositioned nowhere is never filed."
        )

    content = {k: v for k, v in spec.items() if k != "state"}
    return content, state


def _provenance(created_by: str, prompt_ref: str) -> dict[str, Any]:
    """The shape dispatch.provenance_for_operator_action already uses for an
    action that ran no model: ``tokens``/``cost_usd`` are 0/0.0, not None --
    None is that function's distinct meaning for "a worker ran but reported
    no usage", which does not apply to a filing no worker ever touched."""
    return {
        "worker": created_by,
        "model": "none",
        "prompt_ref": prompt_ref,
        "tokens": 0,
        "cost_usd": 0.0,
        "duration_s": 0.0,
    }


@dataclasses.dataclass(frozen=True)
class FiledFinding:
    """Everything a caller of :func:`file_finding` needs to report what happened."""

    id: str
    state: str


def file_finding(
    spec: dict[str, Any],
    created_by: str,
    *,
    prompt_ref: str,
    sub: Any = None,
) -> FiledFinding:
    """File ``spec`` as a dev.finding bead. The one filing path -- CLI and the
    later gateway route both call this rather than each re-deriving the
    refusal checks (PRIN-005, the file_task.file_spec pattern).

    Two writes, not one: ``create_bead`` at the machine's one entry state
    (``pending``), then ``transition_state`` to the spec's disposition.
    There is no atomic "create already-dispositioned" primitive -- the store
    accepts no other entry state for ``("dev", "finding")``.

    If the transition fails, nothing is undone: the created bead stays
    ``pending``, which is itself a valid, findable state. No delete, no
    retry, no second create -- disposition it by hand once whatever the
    store rejected is fixed.
    """
    if not str(created_by or "").strip():
        raise SystemExit("created_by is required and must be non-empty.")
    if not str(prompt_ref or "").strip():
        raise SystemExit("prompt_ref is required and must be non-empty.")

    content, state = _validate_and_build_content(spec)
    provenance = _provenance(created_by, prompt_ref)

    sub = sub if sub is not None else Substrate()
    created = sub.create_bead(
        "dev", "finding", "pending", content, created_by, provenance=provenance
    )

    try:
        sub.transition_state(created["id"], "pending", state, created_by)
    except Exception as exc:  # noqa: BLE001 - report, never hide, a partial write
        print(
            f"filed dev.finding {created['id']} but failed to transition it "
            f"to {state!r}: {exc}\n"
            f"{created['id']} stays pending and findable -- no delete, no "
            "retry, and no second create were attempted. Disposition it by "
            "hand once the underlying conflict clears.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    return FiledFinding(id=created["id"], state=state)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="File a dev.finding bead.")
    parser.add_argument("spec", help="path to a JSON spec, or - for stdin")
    parser.add_argument(
        "--created-by",
        default=None,
        metavar="IDENTITY",
        help="Attribution for this filing. Defaults to $FACTORY_OPERATOR; "
        "refuses if neither is given.",
    )
    parser.add_argument(
        "--prompt-ref",
        required=True,
        metavar="REF",
        help="What session, PR or audit produced this finding.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the create/transition payloads as JSON; make no store call.",
    )
    args = parser.parse_args(argv)

    created_by = args.created_by or os.environ.get("FACTORY_OPERATOR")
    if not created_by:
        raise SystemExit(
            "no --created-by given and FACTORY_OPERATOR is not set in the "
            "environment -- refusing to fabricate an identity. Pass "
            "--created-by, or set FACTORY_OPERATOR."
        )

    prompt_ref = args.prompt_ref
    if not prompt_ref.strip():
        raise SystemExit("--prompt-ref must be non-empty.")

    raw = sys.stdin.read() if args.spec == "-" else open(args.spec).read()
    spec = json.loads(raw)

    if args.dry_run:
        # Deliberately store-free: no Substrate() is ever constructed on this
        # path, not merely "no method is called on one" -- see .factory/design.md.
        content, state = _validate_and_build_content(spec)
        provenance = _provenance(created_by, prompt_ref)
        payload = {
            "create": {
                "namespace": "dev",
                "type": "finding",
                "state": "pending",
                "content": content,
                "created_by": created_by,
                "provenance": provenance,
            },
            "transition": {
                "from_state": "pending",
                "to_state": state,
                "created_by": created_by,
            },
        }
        print(json.dumps(payload, indent=2))
        return 0

    filed = file_finding(spec, created_by, prompt_ref=prompt_ref)
    print(f"{filed.id} {filed.state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
