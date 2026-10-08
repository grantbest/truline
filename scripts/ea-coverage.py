#!/usr/bin/env python3
"""Report EA model linkage coverage against the population a field applies to.

This script measures coverage. It does not make blank fields illegal, does not
change conformance verdicts, and does not require a substrate, cluster, or
network. The core computation is a pure function over already-loaded model
records so tests can exercise applicability rules without touching the repo.

Usage:
    python3 scripts/ea-coverage.py
    python3 scripts/ea-coverage.py --json
    python3 scripts/ea-coverage.py --history /tmp/ea-coverage-history.json
    python3 scripts/ea-coverage.py --check

`--check` is the ratchet: it fails the build when any measured field declines
against COVERAGE_BASELINE below, and passes when every field holds or climbs.
The baseline is a plain constant, computed by running this tool at this
change's own HEAD and committed beside the checker — not hand-typed from a
plan — so recompute and update it in the same PR that intentionally moves
coverage (s35-b5 is expected to do exactly that).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
from collections.abc import Iterable
from typing import Any, Callable

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("pyyaml is required: pip install pyyaml")


REPO = pathlib.Path(__file__).resolve().parent.parent
MODEL_DIR = REPO / "docs" / "architecture" / "model"

FIELD_RULES = {
    "capability.supports": (
        "Applies when content.layer == 'supply'; demand capabilities are the "
        "business-value side, and a missing or unrecognised layer is unknown."
    ),
    "application.realizes": (
        "Applies when bead.state is plan, build, operate, or retire; eol "
        "applications no longer realise current capability, and missing or "
        "unrecognised state is unknown."
    ),
    "application.depends_on_authored": (
        "Applies when content.depends_on names at least one dependency. "
        "Applications with workload.runtime == 'none' are not applicable, and a "
        "missing or unrecognised runtime is unknown. This measures what the "
        "model's authored YAML records, not dependency posture: posture "
        "(known / assessed_none / unknown, plus the coherence case) is the "
        "technology-layer row, from the observer's "
        "ea_dependency.summarize_dependency_posture."
    ),
    "application.loc": (
        "Applies when content.build == 'custom'; build in oss or saas is "
        "not applicable, and missing or unrecognised build is unknown."
    ),
}

_KNOWN_APP_STATES = {"plan", "build", "operate", "retire", "eol"}
_KNOWN_BUILD_TYPES = {"custom", "oss", "saas"}
_KNOWN_LAYERS = {"demand", "supply"}
_KNOWN_RUNTIMES = {"kubernetes", "external", "none"}

# Computed at this change's own HEAD by running:
#   python3 scripts/ea-coverage.py --json
# and reading `observation.coverage.<field>.{covered,applicable}` for each
# field. Honest numbers only — recompute and update this constant in the same
# PR that intentionally moves coverage; do not hand-edit it to make --check
# pass.
COVERAGE_BASELINE: dict[str, dict[str, int]] = {
    "application.depends_on_authored": {"covered": 3, "applicable": 3},
    "application.loc": {"covered": 8, "applicable": 9},
    "application.realizes": {"covered": 25, "applicable": 29},
    "capability.supports": {"covered": 12, "applicable": 42},
}

# Fields whose applicable population had already shrunk against COVERAGE_BASELINE
# before this ratchet (dev.finding 462213e0) existed to catch it. This is a dated
# observation to burn down, not a permanent carve-out -- each field leaves this set
# under its own, different condition:
#
#   application.realizes (baseline 25/29, live 25/28): #821 moved app.goose-agent to
#   state eol, and _classify_application_realizes treats eol as not_applicable, so
#   the applicable population permanently dropped by one. No reconciliation restores
#   it -- this leaves this set only when COVERAGE_BASELINE is next recomputed in a
#   PR that intentionally moves coverage (the baseline's own stated contract above),
#   which absorbs the eol population change into a new honest baseline.
#
# Closed: application.depends_on_authored (previously application.depends_on;
# baseline 13/13, live 3/3) left this set in dev.finding 682ac674 part a, which
# renamed the field and recomputed COVERAGE_BASELINE to the honest 3/3 in the same
# change. The baseline already reflects the post-shrink number, so no exemption
# remains to grant -- a further shrink of this field below 3/3 is a live decline the
# ratchet must catch, not one still covered by history.
PRE_EXISTING_DENOMINATOR_SHRINKS: frozenset[str] = frozenset({"application.realizes"})


class CoverageResult:
    def __init__(
        self,
        field: str,
        rule: str,
        covered: tuple[str, ...],
        missing: tuple[str, ...],
        not_applicable: tuple[str, ...],
        unknown: tuple[str, ...],
    ) -> None:
        self.field = field
        self.rule = rule
        self.covered = covered
        self.missing = missing
        self.not_applicable = not_applicable
        self.unknown = unknown

    @property
    def applicable(self) -> int:
        return len(self.covered) + len(self.missing)

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "rule": self.rule,
            "covered": len(self.covered),
            "applicable": self.applicable,
            "missing": len(self.missing),
            "not_applicable": len(self.not_applicable),
            "unknown": len(self.unknown),
            "covered_refs": list(self.covered),
            "missing_refs": list(self.missing),
            "not_applicable_refs": list(self.not_applicable),
            "unknown_refs": list(self.unknown),
        }


class TechnologyLayerResult:
    """The technology-layer row (PC-ASR-002/AC-2, S52-3, S53-4) -- the one place
    dependency posture is reported.

    Unlike the four git-model rows above, ``arch.ci`` counts and dependency
    posture are runtime facts that exist only in the substrate -- this script
    never reads a cluster or a substrate itself (see module docstring).
    Instead this is a pure function over the standing ``obs.ea-observer-status``
    bead ``activities/ea_observation.py`` already writes every night: its
    ``context.ci_counts`` and ``context.dependency_posture`` fields, when that
    run completed with ``status: ok``. ``context.dependency_posture`` is
    ``apps/factory-dispatcher/activities/ea_dependency.py``'s
    ``summarize_dependency_posture`` output -- the one definition of known /
    assessed_none / unknown and the coherence case (an application whose only
    surviving ``depends_on`` edge targets a non-application); this row mirrors
    it exactly rather than re-deriving it. A caller with no such bead (a local
    ``python3 scripts/ea-coverage.py`` run, or a substrate outage) gets
    ``evaluated=False`` with a stated reason -- never a fabricated zero
    (PRIN-015).
    """

    field = "technology.layer"

    def __init__(
        self,
        *,
        evaluated: bool,
        reason: str | None = None,
        observed_at: str | None = None,
        ci_total: int = 0,
        ci_attributed: int = 0,
        ci_unattributed: int = 0,
        applications_total: int = 0,
        depends_on_known: int = 0,
        depends_on_assessed_none: int = 0,
        depends_on_unknown: int = 0,
        depends_on_coherence_case: int = 0,
    ) -> None:
        self.evaluated = evaluated
        self.reason = reason
        self.observed_at = observed_at
        self.ci_total = ci_total
        self.ci_attributed = ci_attributed
        self.ci_unattributed = ci_unattributed
        self.applications_total = applications_total
        self.depends_on_known = depends_on_known
        self.depends_on_assessed_none = depends_on_assessed_none
        self.depends_on_unknown = depends_on_unknown
        self.depends_on_coherence_case = depends_on_coherence_case

    def as_dict(self) -> dict[str, Any]:
        if not self.evaluated:
            return {
                "field": self.field,
                "evaluated": False,
                "reason": self.reason or "no live observer-status record supplied",
            }
        return {
            "field": self.field,
            "evaluated": True,
            "observed_at": self.observed_at,
            "arch_ci": {
                "total": self.ci_total,
                "attributed": self.ci_attributed,
                "unattributed": self.ci_unattributed,
            },
            "application_depends_on_posture": {
                "applications_total": self.applications_total,
                "known": self.depends_on_known,
                "assessed_none": self.depends_on_assessed_none,
                "unknown": self.depends_on_unknown,
                "coherence_case": self.depends_on_coherence_case,
            },
        }


def measure_technology_coverage(
    observer_status: dict[str, Any] | None,
) -> TechnologyLayerResult:
    """The technology-layer row, from an already-loaded ``obs.ea-observer-status``
    bead (or ``None``) -- a pure function, same contract as ``measure_field``
    above, so it is testable from a fixture dict with no substrate involved.
    """
    if not observer_status:
        return TechnologyLayerResult(
            evaluated=False, reason="no live observer-status record supplied"
        )

    context = observer_status.get("context") or {}
    status = context.get("status")
    if status != "ok":
        return TechnologyLayerResult(
            evaluated=False,
            reason=f"last EA observer run reported status={status!r}, not ok",
        )

    dependency_posture = context.get("dependency_posture")
    if not isinstance(dependency_posture, dict):
        return TechnologyLayerResult(
            evaluated=False,
            reason="observer-status record has no dependency_posture recorded",
        )

    ci_counts = context.get("ci_counts") or {}
    ci_attributed = int(ci_counts.get("attributed") or 0)
    ci_unattributed = int(ci_counts.get("unattributed") or 0)
    content = observer_status.get("content") or {}

    return TechnologyLayerResult(
        evaluated=True,
        observed_at=content.get("observed_at"),
        ci_total=ci_attributed + ci_unattributed,
        ci_attributed=ci_attributed,
        ci_unattributed=ci_unattributed,
        applications_total=int(dependency_posture.get("total_applications") or 0),
        depends_on_known=int(dependency_posture.get("known") or 0),
        depends_on_assessed_none=int(dependency_posture.get("assessed_none") or 0),
        depends_on_unknown=int(dependency_posture.get("unknown") or 0),
        depends_on_coherence_case=len(dependency_posture.get("coherence_case_refs") or ()),
    )


class DenominatorShrink:
    """A field whose applicable population fell between two observations.

    Independent of CoverageDecline: the covered/applicable fraction can hold or
    climb while the population itself shrinks (13/13 -> 3/3 is 1.0 -> 1.0), and a
    fraction comparison structurally cannot see that. This is the second signal.
    """

    def __init__(
        self,
        field: str,
        before_applicable: int,
        after_applicable: int,
    ) -> None:
        self.field = field
        self.before_applicable = before_applicable
        self.after_applicable = after_applicable

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "before_applicable": self.before_applicable,
            "after_applicable": self.after_applicable,
        }


class CoverageDecline:
    def __init__(
        self,
        field: str,
        before_covered: int,
        before_applicable: int,
        after_covered: int,
        after_applicable: int,
    ) -> None:
        self.field = field
        self.before_covered = before_covered
        self.before_applicable = before_applicable
        self.after_covered = after_covered
        self.after_applicable = after_applicable

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "before": {
                "covered": self.before_covered,
                "applicable": self.before_applicable,
            },
            "after": {
                "covered": self.after_covered,
                "applicable": self.after_applicable,
            },
        }


def _load(name: str, key: str) -> list[dict[str, Any]]:
    path = MODEL_DIR / name
    if not path.exists():
        sys.exit(f"model file missing: {path}")
    return yaml.safe_load(path.read_text())[key]


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Iterable) and not isinstance(value, (bytes, dict)):
        return bool(list(value))
    return True


def _record_ref(record: dict[str, Any]) -> str:
    return str(record.get("ref") or "<missing-ref>")


def _classify_capability_supports(record: dict[str, Any]) -> str:
    content = record.get("content") or {}
    layer = content.get("layer")
    if layer == "supply":
        return "covered" if _has_value(content.get("supports")) else "missing"
    if layer == "demand":
        return "not_applicable"
    if layer not in _KNOWN_LAYERS:
        return "unknown"
    raise AssertionError(layer)


def _classify_application_realizes(record: dict[str, Any]) -> str:
    state = record.get("state")
    content = record.get("content") or {}
    if state in {"plan", "build", "operate", "retire"}:
        return "covered" if _has_value(content.get("realizes")) else "missing"
    if state == "eol":
        return "not_applicable"
    if state not in _KNOWN_APP_STATES:
        return "unknown"
    raise AssertionError(state)


def _classify_application_depends_on_authored(record: dict[str, Any]) -> str:
    content = record.get("content") or {}
    if _has_value(content.get("depends_on")):
        return "covered"

    workload = content.get("workload")
    if not isinstance(workload, dict):
        return "unknown"
    runtime = workload.get("runtime")
    if runtime == "none":
        return "not_applicable"
    if runtime in {"kubernetes", "external"}:
        return "unknown"
    if runtime not in _KNOWN_RUNTIMES:
        return "unknown"
    raise AssertionError(runtime)


def _classify_application_loc(record: dict[str, Any]) -> str:
    content = record.get("content") or {}
    build = content.get("build")
    if build == "custom":
        return "covered" if _has_value(content.get("loc")) else "missing"
    if build in {"oss", "saas"}:
        return "not_applicable"
    if build not in _KNOWN_BUILD_TYPES:
        return "unknown"
    raise AssertionError(build)


def _measure_field(
    field: str,
    records: list[dict[str, Any]],
    classifier: Callable[[dict[str, Any]], str],
) -> CoverageResult:
    buckets: dict[str, list[str]] = {
        "covered": [],
        "missing": [],
        "not_applicable": [],
        "unknown": [],
    }
    for record in records:
        state = classifier(record)
        if state not in buckets:
            raise AssertionError(f"{field}: invalid coverage state {state!r}")
        buckets[state].append(_record_ref(record))

    return CoverageResult(
        field=field,
        rule=FIELD_RULES[field],
        covered=tuple(sorted(buckets["covered"])),
        missing=tuple(sorted(buckets["missing"])),
        not_applicable=tuple(sorted(buckets["not_applicable"])),
        unknown=tuple(sorted(buckets["unknown"])),
    )


def measure_model_coverage(
    capabilities: list[dict[str, Any]],
    applications: list[dict[str, Any]],
) -> dict[str, CoverageResult]:
    """Return coverage for every linkage field this report owns."""
    return {
        "capability.supports": _measure_field(
            "capability.supports", capabilities, _classify_capability_supports
        ),
        "application.realizes": _measure_field(
            "application.realizes", applications, _classify_application_realizes
        ),
        "application.depends_on_authored": _measure_field(
            "application.depends_on_authored",
            applications,
            _classify_application_depends_on_authored,
        ),
        "application.loc": _measure_field(
            "application.loc", applications, _classify_application_loc
        ),
    }


def coverage_observation(
    coverage: dict[str, CoverageResult],
    observed_at: dt.date | str | None = None,
    technology: TechnologyLayerResult | None = None,
) -> dict[str, Any]:
    """Create the dated observation that can be retained and compared later.

    ``technology``, when given, adds the ``"technology.layer"`` row alongside
    the git-model fields. Its dict shape (``evaluated``/``arch_ci``/
    ``application_depends_on_posture``, no ``covered``/``applicable`` keys) is
    deliberately outside ``compare_observations``/``compare_denominators``'s
    fraction-and-shrink ratchet -- both already default a missing
    ``applicable`` to 0 on both sides of a comparison and report nothing, so
    this row rides along inertly rather than needing a special case there.
    """
    if observed_at is None:
        observed = dt.date.today().isoformat()
    elif isinstance(observed_at, dt.date):
        observed = observed_at.isoformat()
    else:
        observed = observed_at

    rows = {field: coverage[field].as_dict() for field in sorted(coverage)}
    if technology is not None:
        rows[technology.field] = technology.as_dict()

    return {
        "observed_at": observed,
        "coverage": rows,
    }


def append_observation(
    history: list[dict[str, Any]],
    observation: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return a new retained history with this dated measurement appended."""
    return [*history, observation]


def compare_observations(
    before: dict[str, Any],
    after: dict[str, Any],
) -> list[CoverageDecline]:
    """Return fields whose covered/applicable fraction fell."""
    declines: list[CoverageDecline] = []
    before_cov = before.get("coverage") or {}
    after_cov = after.get("coverage") or {}
    for field in sorted(set(before_cov) & set(after_cov)):
        old = before_cov[field]
        new = after_cov[field]
        old_applicable = int(old.get("applicable") or 0)
        new_applicable = int(new.get("applicable") or 0)
        if old_applicable == 0 or new_applicable == 0:
            continue
        old_covered = int(old.get("covered") or 0)
        new_covered = int(new.get("covered") or 0)
        if new_covered * old_applicable < old_covered * new_applicable:
            declines.append(
                CoverageDecline(
                    field=field,
                    before_covered=old_covered,
                    before_applicable=old_applicable,
                    after_covered=new_covered,
                    after_applicable=new_applicable,
                )
            )
    return declines


def compare_denominators(
    before: dict[str, Any],
    after: dict[str, Any],
) -> list[DenominatorShrink]:
    """Return fields whose applicable population fell, independent of the fraction.

    Never divides, so it has no zero-applicable case to guard: 13/13 -> 0/0 reports
    (13 lost), 0/0 -> 0/0 and 0/0 -> 13/13 stay silent (nothing was lost either way).
    """
    shrinks: list[DenominatorShrink] = []
    before_cov = before.get("coverage") or {}
    after_cov = after.get("coverage") or {}
    for field in sorted(set(before_cov) & set(after_cov)):
        old_applicable = int(before_cov[field].get("applicable") or 0)
        new_applicable = int(after_cov[field].get("applicable") or 0)
        if new_applicable < old_applicable:
            shrinks.append(
                DenominatorShrink(
                    field=field,
                    before_applicable=old_applicable,
                    after_applicable=new_applicable,
                )
            )
    return shrinks


def baseline_observation() -> dict[str, Any]:
    """The committed baseline, in the same shape `compare_observations` expects."""
    return {
        "observed_at": "baseline",
        "coverage": {field: dict(values) for field, values in COVERAGE_BASELINE.items()},
    }


def check_result(observation: dict[str, Any]) -> list[CoverageDecline]:
    """Declines of `observation` against the committed baseline."""
    return compare_observations(baseline_observation(), observation)


def check_denominator_shrinks(observation: dict[str, Any]) -> list[DenominatorShrink]:
    """Denominator shrinks of `observation` against the committed baseline.

    Excludes PRE_EXISTING_DENOMINATOR_SHRINKS: fields already shrunk when this
    signal was added. The fraction rule above is not exempted for those fields --
    only this new signal is.
    """
    shrinks = compare_denominators(baseline_observation(), observation)
    return [s for s in shrinks if s.field not in PRE_EXISTING_DENOMINATOR_SHRINKS]


def _load_history(path: pathlib.Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text())
    if not isinstance(raw, list):
        sys.exit(f"coverage history must be a JSON array: {path}")
    return raw


def _write_history(path: pathlib.Path, history: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(history, indent=2, sort_keys=True) + "\n")


def _format_report(
    observation: dict[str, Any],
    declines: list[CoverageDecline],
    shrinks: Iterable[DenominatorShrink] = (),
) -> str:
    lines = [f"EA linkage coverage observed_at={observation['observed_at']}"]
    for field, result in observation["coverage"].items():
        if "evaluated" in result:
            if not result["evaluated"]:
                lines.append(f"{field}: not evaluated ({result['reason']})")
                continue
            ci = result["arch_ci"]
            posture = result["application_depends_on_posture"]
            lines.append(
                f"{field}: arch.ci {ci['attributed']}/{ci['total']} attributed "
                f"({ci['unattributed']} unattributed); depends_on posture "
                f"{posture['known']} known, {posture['assessed_none']} assessed-none, "
                f"{posture['unknown']} unknown ({posture['coherence_case']} coherence-case) "
                f"of {posture['applications_total']} applications"
            )
            continue
        lines.append(
            f"{field}: {result['covered']}/{result['applicable']} covered/applicable; "
            f"missing {result['missing']}; not-applicable {result['not_applicable']}; "
            f"unknown {result['unknown']}"
        )
    if declines:
        lines.append("")
        lines.append("Coverage decline(s):")
        for decline in declines:
            lines.append(
                f"  {decline.field}: "
                f"{decline.before_covered}/{decline.before_applicable} -> "
                f"{decline.after_covered}/{decline.after_applicable}"
            )
    if shrinks:
        lines.append("")
        lines.append("Denominator shrink(s) — applicable population fell:")
        for shrink in shrinks:
            lines.append(
                f"  {shrink.field}: "
                f"applicable {shrink.before_applicable} -> {shrink.after_applicable}"
            )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", help="observation date, ISO yyyy-mm-dd (default: today)")
    ap.add_argument("--json", action="store_true", help="print the dated observation as JSON")
    ap.add_argument(
        "--history",
        type=pathlib.Path,
        help="append this measurement to a retained JSON history and report declines",
    )
    ap.add_argument(
        "--check", action="store_true",
        help=(
            "fail (exit 1) if any measured field declines against the committed "
            "baseline, or if its applicable population shrank (excluding fields "
            "already shrunk when this check was added; see "
            "PRE_EXISTING_DENOMINATOR_SHRINKS)"
        ),
    )
    ap.add_argument(
        "--after-fixture",
        type=pathlib.Path,
        help=(
            "read the after-state observation from this committed or test-built "
            "JSON file (same shape as coverage_observation()) instead of measuring "
            "the live model; lets --check be verified end-to-end without depending "
            "on the live model's current shape"
        ),
    )
    ap.add_argument(
        "--observer-status",
        type=pathlib.Path,
        help=(
            "path to a JSON dump of the standing obs.ea-observer-status bead "
            "(activities/ea_observation.py) to compute the technology-layer row "
            "from; omitted by default (this script touches no substrate or "
            "cluster on its own), in which case that row reports evaluated=false "
            "rather than a fabricated zero"
        ),
    )
    args = ap.parse_args()

    if args.after_fixture:
        observation = json.loads(args.after_fixture.read_text())
    else:
        capabilities = _load("business-layer.yaml", "capabilities")
        applications = _load("application-portfolio.yaml", "applications")
        observer_status = (
            json.loads(args.observer_status.read_text()) if args.observer_status else None
        )
        observation = coverage_observation(
            measure_model_coverage(capabilities, applications),
            observed_at=args.date,
            technology=measure_technology_coverage(observer_status),
        )

    declines: list[CoverageDecline] = []
    shrinks: list[DenominatorShrink] = []
    if args.history:
        history = _load_history(args.history)
        if history:
            declines = compare_observations(history[-1], observation)
            shrinks = compare_denominators(history[-1], observation)
        _write_history(args.history, append_observation(history, observation))

    check_declines = check_result(observation) if args.check else []
    check_shrinks = check_denominator_shrinks(observation) if args.check else []

    if args.json:
        body: dict[str, Any] = {"observation": observation}
        if declines:
            body["declines"] = [decline.as_dict() for decline in declines]
        if shrinks:
            body["denominator_shrinks"] = [shrink.as_dict() for shrink in shrinks]
        if args.check:
            body["check"] = {
                "baseline": COVERAGE_BASELINE,
                "passed": not check_declines and not check_shrinks,
                "declines": [decline.as_dict() for decline in check_declines],
                "denominator_shrinks": [shrink.as_dict() for shrink in check_shrinks],
            }
        print(json.dumps(body, indent=2, sort_keys=True))
    else:
        print(_format_report(observation, declines, shrinks))
        if args.check:
            if check_declines or check_shrinks:
                print(
                    "\nFAIL — coverage declined or its applicable population shrank "
                    "against the committed baseline:"
                )
                for decline in check_declines:
                    print(
                        f"  {decline.field}: coverage declined "
                        f"{decline.before_covered}/{decline.before_applicable} -> "
                        f"{decline.after_covered}/{decline.after_applicable}"
                    )
                for shrink in check_shrinks:
                    print(
                        f"  {shrink.field}: applicable population shrank "
                        f"{shrink.before_applicable} -> {shrink.after_applicable}"
                    )
            else:
                print(
                    "\nOK — coverage holds or climbs and no field's applicable "
                    "population shrank against the committed baseline."
                )

    if args.check and (check_declines or check_shrinks):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
