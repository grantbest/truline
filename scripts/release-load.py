#!/usr/bin/env python3
"""Mirror release charters from git into the substrate.

`docs/releases/*.json` stays authoritative for release *structure* — the
objective, the outcomes, the declared balance. The substrate gets an idempotent,
queryable mirror as `arch.release` beads keyed by `content.ref`.

**State is deliberately not mirrored.** A charter in git may not declare that a
release shipped: `planned -> in_flight -> closing -> released` is lifecycle, it
is only true once the work landed, and it moves by transition through the
substrate's state machine. So this loader creates at `planned` and never touches
state again — not on the first run, not on any later one. That split is PQ-4
(the Operator, 2026-08-22): git authoritative for structure, substrate authoritative
for lifecycle.

* Dry-run is the default. Writes require `--apply`.
* No delete path exists in this loader.
* Charters are discovered by enumerating the directory, never by a hard-coded
  filename — the same rule `PC-FAC-001/AC-5` puts on the requirement registries,
  and for the same reason: the next charter is picked up with no code change.
* Requirement references are resolved against `docs/requirements/` before
  anything is written. A charter citing a criterion that does not exist would
  make the release notes render a measurement that never happened.
* Every charter is validated against the substrate's own `ArchReleaseContent`
  model (`apps/substrate/src/schemas.py`) before either mode reports on it.
  `--dry-run` printing `create <ref>` for a charter `--apply` would then have
  rejected is exactly the false green this loader must not produce — a
  charter that fails this check is reported as an error, on both paths, and
  never as a plan line (OPS measured 2026-09-12: R26.09 landed with
  `opened_at: null`, dry-run said `create R26.09`, apply's 422 went unread).
* `--check` validates every charter and
  `docs/releases/policy/health-policy.json` (via `health_policy.py`) and
  writes nothing — no `Substrate` is constructed on this path at all, so it
  needs neither `SUBSTRATE_URL` nor `SUBSTRATE_API_KEY`. It is the CI gate
  PC-ASR-008/AC-6 names: a bad policy commit is refused before it reaches
  main, rather than caught only once the reconciler (B13) reports the
  release-health computation unmeasured.

Environment: SUBSTRATE_URL, SUBSTRATE_API_KEY (not read by --check).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import sys
from typing import NamedTuple, Optional

REPO = pathlib.Path(__file__).resolve().parent.parent
RELEASES_DIR = REPO / "docs" / "releases"
REQUIREMENTS_DIR = REPO / "docs" / "requirements"
RELEASE_TYPE = "release"
CREATED_BY = "release-load"
ENTRY_STATE = "planned"

#: Keys a charter may not carry. Kept here as well as in the substrate model so
#: the refusal happens before a network call, with the file name attached.
MEASUREMENT_KEYS = frozenset(
    {"conformance", "verdict", "measured_at", "measured_revision", "actual_balance"}
)

sys.path.insert(0, str(REPO / "scripts"))
import traceability  # noqa: E402
from health_policy import (  # noqa: E402
    HEALTH_POLICY_PATH,
    health_policy_validation_error,
    load_health_policy,
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

# The substrate's own content model, imported rather than re-described here —
# a second, hand-maintained copy of ArchReleaseContent's rules is exactly how
# a dry-run's "valid" and the substrate's "valid" drift apart.
sys.path.insert(0, str(REPO / "apps" / "substrate" / "src"))
from pydantic import ValidationError  # noqa: E402
from schemas import ArchReleaseContent  # noqa: E402


class Substrate(_Substrate):
    def __init__(self) -> None:
        super().__init__(created_by=CREATED_BY)


class ReleaseItem(NamedTuple):
    ref: str
    source: str
    content: dict


class Plan:
    def __init__(self, apply: bool):
        self.apply = apply
        self.creates: list[str] = []
        self.updates: list[str] = []
        self.unchanged: list[str] = []
        self.errors: list[str] = []

    @property
    def dry_run(self) -> bool:
        return not self.apply


def charter_paths(directory: pathlib.Path = RELEASES_DIR) -> list[pathlib.Path]:
    """Every charter in the directory, in a stable order.

    Notes files (``R26.01-notes.md``) live in the same directory and are not
    charters; only ``*.json`` is read.
    """
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.json"))


def release_content(charter: dict) -> dict:
    """The charter as bead content, with the loader's own fields applied.

    ``source_class: authored`` is not cosmetic. It makes the substrate return a
    409 if an automated writer later tries to overwrite a charter
    (``routes.check_source_class_ownership``) — a release objective is written
    by a person, and nothing derived may quietly restate it.
    """
    content = json.loads(json.dumps(charter))
    content.setdefault("source_class", "authored")
    return content


def load_charters(
    paths: Optional[list[pathlib.Path]] = None,
    requirements_dir: pathlib.Path = REQUIREMENTS_DIR,
) -> list[ReleaseItem]:
    """Read, validate and return every charter on disk.

    Exits rather than returning a partial set: a half-loaded release is worse
    than none, because the report that reads it would look complete.
    """
    if paths is None:
        paths = charter_paths()
    registry = traceability.load_registries(requirements_dir)
    if registry.directory_missing:
        sys.exit(
            f"requirements registry not found: {requirements_dir}\n"
            "cannot verify that any charter's requirement references resolve; "
            "this is an unreachable registry, not a citation that fails to "
            "resolve — restore docs/requirements/ before loading charters."
        )

    items: list[ReleaseItem] = []
    seen: dict[str, str] = {}
    for path in paths:
        try:
            charter = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            sys.exit(f"{path.name}: not valid JSON: {exc}")

        ref = charter.get("ref")
        if not ref:
            sys.exit(f"{path.name}: charter declares no ref")
        if ref in seen:
            sys.exit(f"duplicate release ref {ref}: {seen[ref]} and {path.name}")
        seen[ref] = path.name

        forbidden = sorted(MEASUREMENT_KEYS.intersection(charter))
        if forbidden:
            sys.exit(
                f"{path.name}: a charter states intent, not measurement; "
                f"remove {', '.join(forbidden)} — what happened is an arch.observation"
            )

        dangling = [
            ref_value
            for outcome in charter.get("outcomes", [])
            for ref_value in outcome.get("requirement_refs", [])
            if not registry.resolves(ref_value)
        ]
        if dangling:
            sys.exit(
                f"{path.name}: requirement reference(s) resolve to nothing: "
                + ", ".join(sorted(set(dangling)))
                + f"\nsearched: {', '.join(registry.sources) or '(no registries found)'}"
                + "\nAn outcome citing a criterion that does not exist would make the "
                "release notes render a measurement that never happened."
            )

        items.append(
            ReleaseItem(ref=ref, source=path.name, content=release_content(charter))
        )
    return items


def _validation_message(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        loc = ".".join(str(part) for part in error["loc"]) or "(content)"
        parts.append(f"{loc}: {error['msg']}")
    return "; ".join(parts)


def release_content_validation_error(content: dict) -> Optional[str]:
    """``None`` if ``content`` would pass the substrate's own model, else why not.

    Runs ``ArchReleaseContent`` — the same model ``POST /beads`` validates
    against — so a charter this reports as valid cannot then be rejected by
    the substrate for a reason this check did not evaluate.
    """
    try:
        ArchReleaseContent.model_validate(content)
    except ValidationError as exc:
        return _validation_message(exc)
    return None


def _existing_by_ref(sub: SubstrateClient) -> dict[str, dict]:
    existing: dict[str, dict] = {}
    for bead in sub.list_beads(RELEASE_TYPE):
        ref = (bead.get("content") or {}).get("ref")
        if ref:
            existing[ref] = bead
    return existing


def _safe_error(ref: str, action: str, exc: SubstrateError) -> str:
    return f"{ref}: {action} rejected: substrate {exc.status} on {exc.path}"


def reconcile(
    sub: SubstrateClient, items: list[ReleaseItem], apply: bool = False
) -> Plan:
    plan = Plan(apply=apply)
    existing = _existing_by_ref(sub)

    for item in items:
        invalid = release_content_validation_error(item.content)
        if invalid is not None:
            plan.errors.append(f"{item.ref}: invalid: {invalid}")
            continue

        current = existing.get(item.ref)
        if current is None:
            if apply:
                try:
                    sub.create(RELEASE_TYPE, ENTRY_STATE, item.content)
                except SubstrateError as exc:
                    plan.errors.append(_safe_error(item.ref, "create", exc))
                    continue
            plan.creates.append(item.ref)
            continue

        # Content only. The bead's state is the release's lifecycle and this
        # loader has no opinion about it — see the module docstring.
        if current.get("content") == item.content:
            plan.unchanged.append(item.ref)
            continue

        if apply:
            try:
                sub.patch(current["id"], {"content": item.content})
            except SubstrateError as exc:
                plan.errors.append(_safe_error(item.ref, "update", exc))
                continue
        plan.updates.append(item.ref)

    return plan


def report(plan: Plan) -> int:
    head = "DRY RUN - nothing was written" if plan.dry_run else "applied"
    print(f"release-load: {head}")
    print(
        "releases: "
        f"creates={len(plan.creates)} "
        f"updates={len(plan.updates)} "
        f"unchanged={len(plan.unchanged)}"
    )
    for ref in plan.creates:
        print(f"  create   {ref} (at state {ENTRY_STATE})")
    for ref in plan.updates:
        print(f"  update   {ref} (content only; state untouched)")

    if plan.errors:
        print("errors:")
        for error in plan.errors:
            print(f"  {error}")
    return 1 if plan.errors else 0


def _check_policy(policy_path: pathlib.Path) -> tuple[Optional[str], Optional[str]]:
    """Validate ``policy_path`` for ``--check``.

    Returns ``(ok_line, error_line)`` — exactly one is not ``None``. Three
    failure modes are distinguished (missing, not JSON, invalid) because
    PC-ASR-008/AC-6 requires each to name the file and, for an invalid
    policy, the offending key — a single blanket ``except Exception`` would
    blur those into one message.
    """
    try:
        data = policy_path.read_bytes()
    except FileNotFoundError:
        return None, f"health_policy_validation_error: {policy_path}: missing"
    try:
        content = json.loads(data)
    except json.JSONDecodeError as exc:
        return None, f"{policy_path}: not valid JSON: {exc}"

    error = health_policy_validation_error(content, name=str(policy_path))
    if error is not None:
        return None, f"health_policy_validation_error: {error}"

    _, revision = load_health_policy(policy_path)
    return f"ok {policy_path} (revision {revision})", None


def _check(releases_dir: pathlib.Path, policy_path: pathlib.Path) -> int:
    """``--check``: validate every charter and the health policy; write nothing.

    No ``Substrate`` is constructed anywhere on this path — the policy and
    the charters are files, not a store-mirrored model, so there is nothing
    here for a client to diverge from.

    ``load_charters`` alone does not run ``release_content_validation_error``
    — that call lives inside ``reconcile`` — so AC-1's "exits 0 only when
    every charter … validate[s]" would otherwise go unchecked on this path.
    This runs it itself, per charter, without touching ``load_charters`` or
    ``reconcile``.
    """
    paths = charter_paths(releases_dir)
    items = load_charters(paths)  # exits with load_charters' own message on refusal

    ok = True
    for path, item in zip(paths, items):
        invalid = release_content_validation_error(item.content)
        if invalid is not None:
            print(f"{path}: invalid: {invalid}", file=sys.stderr)
            ok = False
            continue
        print(f"ok {path}")

    ok_line, error_line = _check_policy(policy_path)
    if error_line is not None:
        print(error_line, file=sys.stderr)
        ok = False
    else:
        print(ok_line)

    return 0 if ok else 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="write changes")
    mode.add_argument(
        "--dry-run", action="store_true", help="print the plan; change nothing"
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help=(
            "validate every charter and the release-health policy; "
            "write nothing (PC-ASR-008/AC-6)"
        ),
    )
    parser.add_argument(
        "--releases-dir",
        type=pathlib.Path,
        default=None,
        help="charter directory to validate; --check only (default: RELEASES_DIR)",
    )
    parser.add_argument(
        "--policy",
        type=pathlib.Path,
        default=None,
        help="health policy file to validate; --check only (default: HEALTH_POLICY_PATH)",
    )
    args = parser.parse_args(argv)

    if (args.releases_dir is not None or args.policy is not None) and not args.check:
        parser.error("--releases-dir and --policy are only valid with --check")

    if args.check:
        return _check(
            releases_dir=args.releases_dir if args.releases_dir is not None else RELEASES_DIR,
            policy_path=args.policy if args.policy is not None else HEALTH_POLICY_PATH,
        )

    items = load_charters()
    if not items:
        print(f"release-load: no charters found under {RELEASES_DIR}")
        return 0
    plan = reconcile(Substrate(), items, apply=args.apply)
    return report(plan)


if __name__ == "__main__":
    sys.exit(main())
