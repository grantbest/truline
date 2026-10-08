#!/usr/bin/env python3
"""Ties docs/architecture/README.md's machine-checkable prose to the model.

docs/architecture/README.md carries named counts and claims about the EA
model — how many `arch.*` types are registered, how many capabilities /
applications / services the model files declare, how many checks
`scripts/ea-conformance.py` runs, which files exist, and which Temporal
schedules the machinery section names at which cadence
(`apps/factory-dispatcher/schedule_runtime.py`). Nothing enforced any of
that: the page rotted (wrong counts, a "chartered, not built" caveat about
itself) until the 2026-09-07 truth restoration (#688) corrected it by hand.
That correction is a one-time fix unless something re-checks it on every PR.

Every assertion this module can evaluate is declared in ``ASSERTIONS``, in
one place (PRIN-003). An assertion whose sentence has changed shape and can
no longer be parsed out of the README is a *failure*, not a silent skip
(PRIN-015) — the alternative is a check that quietly stops checking anything
the moment the prose around it is edited.
"""

from __future__ import annotations

import ast
import pathlib
import re
from dataclasses import dataclass
from typing import Callable

import repo_invariants

README_PATH = pathlib.Path("docs/architecture/README.md")
SCHEMAS_PATH = pathlib.Path("apps/substrate/src/schemas.py")
EA_CONFORMANCE_PATH = pathlib.Path("scripts/ea-conformance.py")
BUSINESS_LAYER_PATH = pathlib.Path("docs/architecture/model/business-layer.yaml")
APPLICATION_PORTFOLIO_PATH = pathlib.Path("docs/architecture/model/application-portfolio.yaml")
SERVICES_PATH = pathlib.Path("docs/architecture/model/services.yaml")
SCHEDULE_RUNTIME_PATH = pathlib.Path("apps/factory-dispatcher/schedule_runtime.py")

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
}


@dataclass(frozen=True)
class Assertion:
    """One README claim this checker can evaluate against a model file.

    ``evaluate`` returns violation messages (empty means the claim holds). It
    must never return an empty list merely because it failed to parse the
    README's prose or the model — that is a violation in its own right
    (PRIN-015): say so, don't go quiet.
    """

    id: str
    description: str
    evaluate: Callable[[pathlib.Path, str], list[str]]


def _assign_target_name(node: ast.AST) -> str | None:
    """Name bound by a simple ``NAME = ...`` or ``NAME: T = ...`` statement.

    Handles ``ast.Assign`` and ``ast.AnnAssign`` identically so callers don't
    each re-derive their own copy of this rule -- two independent spellings
    of it in this file (one here, one that used to live only in
    ``_schedule_declarations``) is how an annotated declaration escaping the
    schedule walker arose in the first place.
    """
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        return node.targets[0].id
    return None


def _arch_type_names(root: pathlib.Path) -> frozenset[str] | None:
    """Type names in ``ARCH_TYPE_SCHEMAS``, read statically (no pydantic import)."""
    path = root / SCHEMAS_PATH
    if not path.exists():
        return None
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        target_name = _assign_target_name(node)
        if target_name is None:
            continue
        if target_name != "ARCH_TYPE_SCHEMAS" or not isinstance(node.value, ast.Dict):
            continue
        names: set[str] = set()
        for key in node.value.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                names.add(key.value)
        return frozenset(names)
    return None


_ARCH_TYPES_RE = re.compile(
    r"(?P<count_word>[A-Za-z]+) `arch\.\*` types are registered "
    r"\(`apps/substrate/src/schemas\.py`, `ARCH_TYPE_SCHEMAS`\):\s*"
    r"(?P<names>.*?)\.\s*Their liveness",
    re.DOTALL,
)


def _assert_arch_types(root: pathlib.Path, text: str) -> list[str]:
    match = _ARCH_TYPES_RE.search(text)
    if not match:
        return [
            "could not find the '<N> `arch.*` types are registered (...): "
            "...' sentence in docs/architecture/README.md -- the prose "
            "changed shape and this assertion can no longer be evaluated; "
            "update _ARCH_TYPES_RE in scripts/readme_conformance.py or "
            "restore the sentence"
        ]

    actual_names = _arch_type_names(root)
    if actual_names is None:
        return [
            f"{SCHEMAS_PATH} not found or declares no ARCH_TYPE_SCHEMAS dict "
            "-- cannot verify the README's `arch.*` type claim"
        ]

    messages: list[str] = []
    count_word = match.group("count_word").strip().lower()
    claimed_count = _NUMBER_WORDS.get(count_word)
    if claimed_count is None:
        messages.append(
            f"README says {match.group('count_word')!r} `arch.*` types are "
            "registered, which is not a recognized number word -- cannot "
            "verify the count"
        )
    elif claimed_count != len(actual_names):
        messages.append(
            f"README says {match.group('count_word')} ({claimed_count}) "
            f"`arch.*` types are registered, but {SCHEMAS_PATH} "
            f"ARCH_TYPE_SCHEMAS carries {len(actual_names)}: "
            f"{', '.join(sorted(actual_names))}"
        )

    claimed_names = frozenset(re.findall(r"`([a-z_]+)`", match.group("names")))
    if claimed_names != actual_names:
        detail = []
        missing = actual_names - claimed_names
        extra = claimed_names - actual_names
        if missing:
            detail.append(f"missing from README: {', '.join(sorted(missing))}")
        if extra:
            detail.append(f"named in README but not in ARCH_TYPE_SCHEMAS: {', '.join(sorted(extra))}")
        messages.append(
            "README's `arch.*` type list does not match "
            f"{SCHEMAS_PATH} ARCH_TYPE_SCHEMAS ({'; '.join(detail)})"
        )
    return messages


def _count_from_yaml(root: pathlib.Path, rel_path: pathlib.Path, key: str) -> int | None:
    path = root / rel_path
    if not path.exists():
        return None
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    items = data.get(key)
    if not isinstance(items, list):
        return None
    return len(items)


def _model_count_assertion(
    model_path: pathlib.Path,
    yaml_key: str,
    row_re: re.Pattern[str],
    label: str,
) -> Callable[[pathlib.Path, str], list[str]]:
    def evaluate(root: pathlib.Path, text: str) -> list[str]:
        match = row_re.search(text)
        if not match:
            return [
                f"could not find the {label} count in the 'The files' table "
                f"row for {model_path.as_posix()} -- the row changed shape "
                "and this assertion can no longer be evaluated; update the "
                "pattern in scripts/readme_conformance.py or restore the row"
            ]
        claimed = int(match.group("n"))
        actual = _count_from_yaml(root, model_path, yaml_key)
        if actual is None:
            return [
                f"{model_path.as_posix()} not found or declares no "
                f"'{yaml_key}' list -- cannot verify the README's count"
            ]
        if claimed != actual:
            return [
                f"README's files table claims {claimed} {label} in "
                f"{model_path.as_posix()}, but it declares {actual}"
            ]
        return []

    return evaluate


_BUSINESS_LAYER_ROW_RE = re.compile(r"model/business-layer\.yaml\)\s*\|\s*(?P<n>\d+) capabilities")
_APPLICATION_PORTFOLIO_ROW_RE = re.compile(r"model/application-portfolio\.yaml\)\s*\|\s*(?P<n>\d+) applications")
_SERVICES_ROW_RE = re.compile(r"model/services\.yaml\)\s*\|\s*(?P<n>\d+) application services")

_EA_CHECKS_RE = re.compile(r"`scripts/ea-conformance\.py`\s*[-–—]\s*(?P<n>\d+) checks")
_CHECK_DEF_RE = re.compile(r"^def check_", re.MULTILINE)


def _assert_ea_conformance_checks(root: pathlib.Path, text: str) -> list[str]:
    match = _EA_CHECKS_RE.search(text)
    if not match:
        return [
            "could not find the '`scripts/ea-conformance.py` -- <N> checks' "
            "claim in docs/architecture/README.md -- the prose changed shape "
            "and this assertion can no longer be evaluated; update "
            "_EA_CHECKS_RE in scripts/readme_conformance.py or restore the "
            "claim"
        ]
    claimed = int(match.group("n"))
    path = root / EA_CONFORMANCE_PATH
    if not path.exists():
        return [f"{EA_CONFORMANCE_PATH} not found -- cannot verify the README's check count"]
    actual = len(_CHECK_DEF_RE.findall(path.read_text(encoding="utf-8")))
    if claimed != actual:
        return [
            f"README claims {claimed} checks in {EA_CONFORMANCE_PATH}, but "
            f"it defines {actual} check_* function(s)"
        ]
    return []


_FILES_TABLE_LINK_RE = re.compile(r"\[`([^`]+)`\]\(([^)]+)\)")


def _assert_file_inventory(root: pathlib.Path, text: str) -> list[str]:
    _, sep, after = text.partition("## The files")
    if not sep:
        return ["could not find the '## The files' section in docs/architecture/README.md"]
    section, _, _ = after.partition("\n## ")
    rows = _FILES_TABLE_LINK_RE.findall(section)
    if not rows:
        return [
            "'## The files' table lists no linked files -- cannot verify the "
            "file inventory claim"
        ]
    messages: list[str] = []
    for label, target in rows:
        rel = pathlib.Path("docs/architecture") / target
        if not (root / rel).exists():
            messages.append(
                f"README's files table lists {label} at {target}, but "
                f"{rel.as_posix()} does not exist"
            )
    return messages


_SCHEDULE_ID_SUFFIX = "_SCHEDULE_ID"
_SCHEDULE_INTERVAL_SUFFIX = "_SCHEDULE_INTERVAL_SECONDS"


def _eval_int_literal(node: ast.expr, namespace: dict[str, int]) -> int | None:
    """Evaluate the narrow subset of integer expressions schedule_runtime.py
    actually uses: int literals, `+`/`-`/`*` between them, and a bare name
    referencing an earlier constant in the same module (module order is a
    real dependency here, not incidental -- an alias like
    ``RELEASE_APPLY_SCHEDULE_INTERVAL_SECONDS = EA_APPLY_SCHEDULE_INTERVAL_SECONDS``
    only resolves because ``EA_APPLY_SCHEDULE_INTERVAL_SECONDS`` was recorded
    into ``namespace`` on an earlier statement in the same walk).
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.Name):
        return namespace.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mult, ast.Add, ast.Sub)):
        left = _eval_int_literal(node.left, namespace)
        right = _eval_int_literal(node.right, namespace)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Add):
            return left + right
        return left - right
    return None


@dataclass(frozen=True)
class ScheduleDeclarations:
    """What ``schedule_runtime.py`` declares, read statically (see
    ``_arch_type_names`` for why: this must evaluate against a synthetic
    fixture repo, not the real installed module).
    """

    #: schedule_id -> interval_seconds, for every "<PREFIX>_SCHEDULE_ID" /
    #: "<PREFIX>_SCHEDULE_INTERVAL_SECONDS" pair whose interval resolved.
    intervals_by_id: dict[str, int]
    #: schedule_id for every "<PREFIX>_SCHEDULE_ID" whose sibling interval
    #: constant is missing or could not be resolved -- tracked, not dropped,
    #: so a README claim about one of these still fails as cannot-evaluate
    #: instead of silently finding nothing to compare against (PRIN-015).
    unresolved_ids: tuple[str, ...]

    @property
    def all_ids(self) -> frozenset[str]:
        return frozenset(self.intervals_by_id) | frozenset(self.unresolved_ids)


def _schedule_declarations(root: pathlib.Path) -> ScheduleDeclarations | None:
    path = root / SCHEDULE_RUNTIME_PATH
    if not path.exists():
        return None
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    namespace: dict[str, int] = {}
    ids: dict[str, str] = {}
    intervals: dict[str, int] = {}
    for node in tree.body:
        name = _assign_target_name(node)
        if name is None or node.value is None:
            continue
        if name.endswith(_SCHEDULE_ID_SUFFIX) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            ids[name[: -len(_SCHEDULE_ID_SUFFIX)]] = node.value.value
        elif name.endswith(_SCHEDULE_INTERVAL_SUFFIX):
            value = _eval_int_literal(node.value, namespace)
            if value is not None:
                namespace[name] = value
                intervals[name[: -len(_SCHEDULE_INTERVAL_SUFFIX)]] = value
    if not ids:
        return None
    return ScheduleDeclarations(
        intervals_by_id={
            schedule_id: intervals[prefix] for prefix, schedule_id in ids.items() if prefix in intervals
        },
        unresolved_ids=tuple(sorted(schedule_id for prefix, schedule_id in ids.items() if prefix not in intervals)),
    )


_MACHINERY_BULLET_RE = re.compile(
    r"^- \*\*(?P<cadence>.+?):\*\*\s*(?P<body>.*?)(?=\n- \*\*|\Z)",
    re.DOTALL | re.MULTILINE,
)
_SCHEDULE_ID_MENTION_RE = re.compile(r"`(factory-[a-z0-9-]+)`")

_CADENCE_UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 24 * 60 * 60}
_CADENCE_EVERY_RE = re.compile(r"every (?P<n>\d+) (?P<unit>second|minute|hour|day)s?\b")


def _parse_cadence_seconds(phrase: str) -> int | None:
    phrase = phrase.strip().lower()
    match = _CADENCE_EVERY_RE.search(phrase)
    if match:
        return int(match.group("n")) * _CADENCE_UNIT_SECONDS[match.group("unit")]
    if "nightly" in phrase or "daily" in phrase:
        return _CADENCE_UNIT_SECONDS["day"]
    return None


#: The lead-in line for the README's own reasoned-exemption list (AC #2): a
#: schedule `schedule_runtime.py` declares that the machinery section does
#: not name, together with why not. Kept as README prose rather than a
#: constant in this module so the reason is visible to a reader of the page
#: itself, and so a test fixture can declare its own exemptions without
#: colliding with the real repository's list (see
#: scripts/tests/test_readme_conformance.py).
_EXEMPT_HEADER = "Schedules `schedule_runtime.py` declares that this page does not name above, and why:"
_EXEMPT_BLOCK_RE = re.compile(
    re.escape(_EXEMPT_HEADER) + r"\s*\n(?P<items>(?:[ \t]*- `factory-[a-z0-9-]+` — [^\n]+\n?)+)"
)
_EXEMPT_ITEM_RE = re.compile(r"- `(?P<id>factory-[a-z0-9-]+)` — (?P<reason>[^\n]+)")


def _machinery_schedule_exemptions(text: str) -> dict[str, str]:
    """Parse the reasoned-exemption list out of the README's own prose.

    Absence of the header is a valid state (nothing is exempted yet) — the
    absent-schedule check in `_assert_machinery_schedules` still reports a
    real gap either way, so this never needs to distinguish "no list" from
    "empty list" as a failure.
    """
    match = _EXEMPT_BLOCK_RE.search(text)
    if not match:
        return {}
    return {
        item.group("id"): item.group("reason").strip()
        for item in _EXEMPT_ITEM_RE.finditer(match.group("items"))
    }


def _assert_machinery_schedules(root: pathlib.Path, text: str) -> list[str]:
    declarations = _schedule_declarations(root)
    if declarations is None:
        return [
            f"{SCHEDULE_RUNTIME_PATH} not found or declares no "
            "'<PREFIX>_SCHEDULE_ID' constants -- cannot verify the README's "
            "schedule/cadence claims"
        ]

    _, sep, after = text.partition("## The machinery (what actually runs)")
    if not sep:
        return [
            "could not find the '## The machinery (what actually runs)' "
            "section in docs/architecture/README.md -- the prose changed "
            "shape and this assertion can no longer be evaluated; update "
            "scripts/readme_conformance.py or restore the section"
        ]
    section, _, _ = after.partition("\n## ")

    messages: list[str] = []
    named_ids: set[str] = set()
    for bullet in _MACHINERY_BULLET_RE.finditer(section):
        cadence_phrase = bullet.group("cadence").strip()
        body = bullet.group("body")
        for schedule_id in _SCHEDULE_ID_MENTION_RE.findall(body):
            named_ids.add(schedule_id)
            if schedule_id in declarations.unresolved_ids:
                messages.append(
                    f"README names schedule `{schedule_id}`, but "
                    f"{SCHEDULE_RUNTIME_PATH} does not declare a resolvable "
                    f"'<PREFIX>_SCHEDULE_INTERVAL_SECONDS' for it -- cannot "
                    "verify the stated cadence"
                )
                continue
            if schedule_id not in declarations.intervals_by_id:
                messages.append(
                    f"README's machinery section names schedule "
                    f"`{schedule_id}`, but {SCHEDULE_RUNTIME_PATH} declares "
                    "no such schedule (renamed or retired?)"
                )
                continue
            expected_seconds = _parse_cadence_seconds(cadence_phrase)
            if expected_seconds is None:
                messages.append(
                    f"could not parse a cadence in seconds from README's "
                    f"{cadence_phrase!r} claim for schedule `{schedule_id}` "
                    f"-- cannot verify it against {SCHEDULE_RUNTIME_PATH}'s "
                    "declared interval"
                )
                continue
            actual_seconds = declarations.intervals_by_id[schedule_id]
            if expected_seconds != actual_seconds:
                messages.append(
                    f"README says schedule `{schedule_id}` runs "
                    f"{cadence_phrase!r} ({expected_seconds}s), but "
                    f"{SCHEDULE_RUNTIME_PATH} declares its interval as "
                    f"{actual_seconds}s"
                )

    exempted = _machinery_schedule_exemptions(text)

    for schedule_id in sorted(declarations.all_ids):
        if schedule_id in named_ids or schedule_id in exempted:
            continue
        messages.append(
            f"{SCHEDULE_RUNTIME_PATH} declares schedule `{schedule_id}`, but "
            "it is named neither in the README's machinery section nor in "
            f"its reasoned-exemption list ({_EXEMPT_HEADER!r}) -- name it in "
            "the section or add a reasoned exemption"
        )

    for schedule_id, reason in exempted.items():
        if not reason:
            messages.append(
                f"README's reasoned-exemption list has no reason for "
                f"`{schedule_id}` -- an exemption must be reasoned, not "
                "silent"
            )
        if schedule_id not in declarations.all_ids:
            messages.append(
                f"README's reasoned-exemption list names `{schedule_id}`, "
                f"which {SCHEDULE_RUNTIME_PATH} no longer declares -- remove "
                "the stale exemption"
            )

    return messages


#: Every README assertion this checker evaluates, declared once. An
#: assertion this repository's prose makes that is not on this list is not
#: enforced -- that gap is real, not hidden, and the file's own docstring
#: says so.
ASSERTIONS: tuple[Assertion, ...] = (
    Assertion(
        "arch-types",
        "the `arch.*` type count and names match ARCH_TYPE_SCHEMAS",
        _assert_arch_types,
    ),
    Assertion(
        "business-layer-capability-count",
        "the business-layer.yaml capability count",
        _model_count_assertion(BUSINESS_LAYER_PATH, "capabilities", _BUSINESS_LAYER_ROW_RE, "capabilities"),
    ),
    Assertion(
        "application-portfolio-count",
        "the application-portfolio.yaml application count",
        _model_count_assertion(APPLICATION_PORTFOLIO_PATH, "applications", _APPLICATION_PORTFOLIO_ROW_RE, "applications"),
    ),
    Assertion(
        "services-count",
        "the services.yaml application service count",
        _model_count_assertion(SERVICES_PATH, "services", _SERVICES_ROW_RE, "application services"),
    ),
    Assertion(
        "ea-conformance-checks-count",
        "the ea-conformance.py check count",
        _assert_ea_conformance_checks,
    ),
    Assertion(
        "file-inventory",
        "every file the 'The files' table names actually exists",
        _assert_file_inventory,
    ),
    Assertion(
        "machinery-schedules",
        "the machinery section's named schedule IDs and cadences match schedule_runtime.py",
        _assert_machinery_schedules,
    ),
)


def check_readme_model_conformance(root: pathlib.Path) -> repo_invariants.RuleResult:
    """README's named counts and claims match the model files they describe."""
    root = root.resolve()
    path = root / README_PATH
    rule_id = "readme-model-conformance"
    name = "README's named counts and claims match the model files they describe"
    if not path.exists():
        return repo_invariants.RuleResult(
            rule_id, name, f"skipped: {README_PATH} not present", ()
        )

    text = path.read_text(encoding="utf-8")
    violations: list[repo_invariants.Violation] = []
    for assertion in ASSERTIONS:
        for message in assertion.evaluate(root, text):
            violations.append(repo_invariants.Violation(message, README_PATH))

    return repo_invariants.RuleResult(
        rule_id,
        name,
        f"{len(ASSERTIONS)} README assertion(s) checked against the model",
        tuple(violations),
    )
