#!/usr/bin/env python3
"""No household identifier, credential-shaped value, or personal financial
field may reach a publishable artifact (PC-TRU-002/AC-1, AC-2; R26.03/O-5).

R26.03/O-4 moves this household's identifiers behind configuration; nothing
previously checked that they stay there, and nothing checked that a personal
financial field never reaches something publishable. secret-scan.yml and
GitGuardian look for credential-shaped values — a hostname, a family
surname, or a financial field name is none of those, and every existing
check passes it.

Publication is a one-way door: once a repository is public, a household
identifier in its history stays public even after the file is deleted. This
check exists to run on every pull request, via check-repo-invariants.py's
existing required job, rather than as a reading performed on publication
day.

Two declared, reviewable inputs do the work so the next identifier is added
without a code change:

  * scripts/private-identifier-patterns.yaml — the patterns to look for.
  * scripts/publishable-scope.yaml — what counts as "publishable" (default:
    everything; a path is excluded only by a declared, reviewed reason).

Plus one ratchet, scripts/private-identifier-grandfathered.txt, naming
pre-existing debt pending R26.03/O-4 so it stays visible instead of hiding
inside a directory exclusion.

Where either declared input exists but does not parse into its expected
shape, this check fails rather than skips: an unreadable policy is exactly
the "cannot determine whether something is publishable" case that must fail
closed, not pass quietly.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass

import repo_invariants

PATTERNS_FILE = pathlib.Path("scripts/private-identifier-patterns.yaml")
SCOPE_FILE = pathlib.Path("scripts/publishable-scope.yaml")
GRANDFATHER_FILE = pathlib.Path("scripts/private-identifier-grandfathered.txt")
RELEASES_DIR = pathlib.Path("docs/releases")

#: These three files declare or ratchet the check itself. PATTERNS_FILE in
#: particular carries this household's real identifiers as pattern text, so
#: scanning it would trip its own rule; see the NOTE in that file for why
#: the exemption lives here (structural) rather than in
#: publishable-scope.yaml (policy, and therefore reviewable/removable).
_SELF_FILES = frozenset(
    {PATTERNS_FILE.as_posix(), SCOPE_FILE.as_posix(), GRANDFATHER_FILE.as_posix()}
)

_VALID_CATEGORIES = frozenset({"household_identifier", "financial_field", "credential"})

#: Mirrors repo_invariants._TEST_SCAN_EXCLUDED_DIRS-style pruning for the
#: os.walk fallback used against a synthetic tree that carries no .git.
_SCAN_EXCLUDED_DIRS = frozenset(
    {".git", ".claude", "venv", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".hypothesis"}
)


@dataclass(frozen=True)
class Pattern:
    name: str
    category: str
    regex: re.Pattern[str]


@dataclass(frozen=True)
class ScopeRule:
    glob: str
    reason: str


@dataclass(frozen=True)
class PublicationTarget:
    name: str
    outcome: str
    globs: tuple[str, ...]


@dataclass(frozen=True)
class ShadowDebtEntry:
    path: str
    reason: str
    clears: tuple[str, ...]


#: A release-prefixed outcome reference, e.g. "R26.05/O-4" — the only shape
#: a publication_targets `outcome` field is allowed to take.
_STRICT_OUTCOME_REF_RE = re.compile(r"^R\d{2}\.\d{2}/O-\d+$")

#: Any outcome-shaped token in prose, prefix optional, so a bare "O-5" is
#: still found (and flagged) alongside a proper "R26.05/O-5".
_OUTCOME_REF_RE = re.compile(r"(?:(R\d{2}\.\d{2})/)?\b(O-\d+)\b")


def _read_yaml(path: pathlib.Path):
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_patterns(root: pathlib.Path) -> list[Pattern] | None:
    """The declared pattern list, or None if PATTERNS_FILE is absent.

    None is tolerated (mirrors house precedent: check_test_map_covers_apps,
    check_job_classes_complete) so a synthetic fixture tree built for an
    unrelated rule's test, which carries no scripts/ contract files at all,
    is not accused of a violation it says nothing about.

    A file that IS present but does not parse into the declared shape raises
    ValueError: that is the "cannot determine" case, and the caller must
    fail on it rather than skip.
    """
    path = root / PATTERNS_FILE
    if not path.exists():
        return None
    data = _read_yaml(path) or {}
    raw = data.get("patterns")
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{PATTERNS_FILE} declares no patterns")
    patterns: list[Pattern] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError(f"{PATTERNS_FILE} has a non-mapping pattern entry: {entry!r}")
        name = entry.get("name")
        category = entry.get("category")
        pattern = entry.get("pattern")
        if not name or category not in _VALID_CATEGORIES or not pattern:
            raise ValueError(f"{PATTERNS_FILE} entry is incomplete or invalid: {entry!r}")
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"{PATTERNS_FILE} pattern {name!r} does not compile: {exc}") from exc
        patterns.append(Pattern(name=name, category=category, regex=regex))
    return patterns


def load_excluded_paths(root: pathlib.Path) -> list[ScopeRule] | None:
    """The declared publishable-scope exclusions, or None if SCOPE_FILE is absent."""
    path = root / SCOPE_FILE
    if not path.exists():
        return None
    data = _read_yaml(path) or {}
    raw = data.get("excluded_paths")
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{SCOPE_FILE} declares no excluded_paths")
    rules: list[ScopeRule] = []
    for entry in raw:
        if not isinstance(entry, dict) or not entry.get("path") or not entry.get("reason"):
            raise ValueError(f"{SCOPE_FILE} has an incomplete entry: {entry!r}")
        rules.append(ScopeRule(glob=str(entry["path"]), reason=str(entry["reason"])))
    return rules


def load_publication_targets(root: pathlib.Path) -> list[PublicationTarget]:
    """Declared publication targets, or [] if SCOPE_FILE has none.

    Unlike excluded_paths, absence of the whole `publication_targets` key is
    tolerated as "no targets declared" rather than an error — every scope
    file written before this key existed must keep working unmodified. A
    present-but-malformed section still fails closed, same as every other
    declared input in this module.
    """
    path = root / SCOPE_FILE
    if not path.exists():
        return []
    data = _read_yaml(path) or {}
    raw = data.get("publication_targets")
    if raw is None:
        return []
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{SCOPE_FILE} declares an empty or non-list publication_targets")
    targets: list[PublicationTarget] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError(f"{SCOPE_FILE} has a non-mapping publication_targets entry: {entry!r}")
        name = entry.get("name")
        outcome = entry.get("outcome")
        paths = entry.get("paths")
        if not name or not outcome or not isinstance(paths, list) or not paths:
            raise ValueError(f"{SCOPE_FILE} publication_targets entry is incomplete: {entry!r}")
        if not _STRICT_OUTCOME_REF_RE.fullmatch(str(outcome)):
            raise ValueError(
                f"{SCOPE_FILE} publication_targets entry {name!r} has outcome "
                f"{outcome!r} that is not release-prefixed (expected e.g. 'R26.05/O-4')"
            )
        targets.append(
            PublicationTarget(name=str(name), outcome=str(outcome), globs=tuple(str(p) for p in paths))
        )
    return targets


def load_accepted_shadow_debt(root: pathlib.Path) -> dict[str, ShadowDebtEntry]:
    """Declared, dated exceptions to "a publication target's paths are never
    shadowed" — keyed by repo-relative path, or {} if SCOPE_FILE declares
    none. Same absence/malformed handling as load_publication_targets."""
    path = root / SCOPE_FILE
    if not path.exists():
        return {}
    data = _read_yaml(path) or {}
    raw = data.get("accepted_shadow_debt")
    if raw is None:
        return {}
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{SCOPE_FILE} declares an empty or non-list accepted_shadow_debt")
    entries: dict[str, ShadowDebtEntry] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError(f"{SCOPE_FILE} has a non-mapping accepted_shadow_debt entry: {entry!r}")
        entry_path = entry.get("path")
        reason = entry.get("reason")
        clears = entry.get("clears")
        if not entry_path or not reason or not isinstance(clears, list) or not clears:
            raise ValueError(f"{SCOPE_FILE} accepted_shadow_debt entry is incomplete: {entry!r}")
        entries[str(entry_path)] = ShadowDebtEntry(
            path=str(entry_path), reason=str(reason), clears=tuple(str(c) for c in clears)
        )
    return entries


def load_grandfathered_paths(root: pathlib.Path) -> set[str]:
    """Ratcheted paths admitted to carry a household identifier already.

    Absent file means an empty set, matching CHANGE_KIND_GRANDFATHER's own
    precedent: this checker also runs against fixture directories that carry
    no ratchet at all, and a missing file there must not be an error.
    """
    path = root / GRANDFATHER_FILE
    if not path.exists():
        return set()
    paths: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.split("#", 1)[0].strip()
        if entry:
            paths.add(entry)
    return paths


def _load_ci_changes():
    """The one implementation of this repository's `**`/`*` glob semantics.

    Loaded the same way check-repo-invariants.py's own `_load_ci_changes`
    loads it: by file path, not by package name, since scripts/ is not a
    package and ci_changes.py is a sibling module rather than a dependency.
    """
    path = pathlib.Path(__file__).resolve().with_name("ci_changes.py")
    spec = importlib.util.spec_from_file_location("ci_changes_for_private_identifiers", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _iter_candidate_files(root: pathlib.Path) -> Iterable[tuple[pathlib.Path, str]]:
    """(absolute path, repo-relative posix path) for every file this check
    could plausibly scan.

    Tracked files via `git ls-files` when root is a git work tree — a
    publishable artifact is a repository object, and ls-files is what
    actually ships. Falls back to a pruned os.walk for a synthetic tree
    built by this module's own tests, which carries no .git.
    """
    if (root / ".git").exists():
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                rel = line.strip()
                if rel:
                    yield root / rel, rel
            return

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SCAN_EXCLUDED_DIRS]
        for filename in filenames:
            abs_path = pathlib.Path(dirpath) / filename
            yield abs_path, abs_path.relative_to(root).as_posix()


#: File suffixes the scanner declares it cannot read as text and is allowed
#: to skip — the closed set the fail-closed rule below checks against
#: (release-gate finding on #643: a bare except swallowing UnicodeDecodeError
#: let ANY unreadable file pass silently, which for a security scanner is
#: coverage quietly lying). Binary formats only; anything else that fails to
#: decode is a violation, not a skip.
DECLARED_UNSCANNABLE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".tgz",
     ".tar", ".woff", ".woff2", ".ttf", ".pyc", ".so", ".dylib", ".patch"}
)


class UnscannableFileError(RuntimeError):
    """A file the scanner could not read and whose type is not declared skippable."""


def _scan_content(path: pathlib.Path, patterns: list[Pattern]) -> list[tuple[Pattern, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        if path.suffix.lower() in DECLARED_UNSCANNABLE_SUFFIXES:
            return []
        raise UnscannableFileError(
            f"{path} could not be read as text ({type(exc).__name__}) and its "
            f"type is not in DECLARED_UNSCANNABLE_SUFFIXES — the scanner fails "
            "closed rather than silently passing a file it never looked at"
        ) from exc
    hits: list[tuple[Pattern, str]] = []
    for pattern in patterns:
        match = pattern.regex.search(text)
        if match:
            hits.append((pattern, match.group(0)))
    return hits


def check_publishable_paths_carry_no_private_identifiers(
    root: pathlib.Path,
) -> repo_invariants.RuleResult:
    rule_id = "publishable-private-identifiers"
    name = "publishable paths carry no household identifier, credential, or financial field"

    try:
        patterns = load_patterns(root)
    except ValueError as exc:
        return repo_invariants.RuleResult(
            rule_id,
            name,
            "declared pattern list could not be read",
            (repo_invariants.Violation(str(exc), PATTERNS_FILE),),
        )
    if patterns is None:
        return repo_invariants.RuleResult(
            rule_id, name, "skipped: no declared pattern list to enforce", ()
        )

    try:
        excluded = load_excluded_paths(root)
    except ValueError as exc:
        return repo_invariants.RuleResult(
            rule_id,
            name,
            "publishable-scope declaration could not be read",
            (repo_invariants.Violation(str(exc), SCOPE_FILE),),
        )
    if excluded is None:
        return repo_invariants.RuleResult(
            rule_id, name, "skipped: no publishable-scope declaration to enforce", ()
        )

    try:
        targets = load_publication_targets(root)
        shadow_debt = load_accepted_shadow_debt(root)
    except ValueError as exc:
        return repo_invariants.RuleResult(
            rule_id,
            name,
            "publishable-scope declaration could not be read",
            (repo_invariants.Violation(str(exc), SCOPE_FILE),),
        )

    grandfathered = load_grandfathered_paths(root)
    ci_changes = _load_ci_changes()
    excluded_rules = list(zip(excluded, [ci_changes.glob_to_regex(rule.glob) for rule in excluded]))
    target_rules = [
        (target, [ci_changes.glob_to_regex(g) for g in target.globs]) for target in targets
    ]

    violations: list[repo_invariants.Violation] = []
    #: accepted_shadow_debt entries reached as a LIVE shadow. An entry that is
    #: never reached is an orphan: the shadow it was declared against is gone, so
    #: the stale check inside the shadow branch can no longer see it. That is the
    #: state the arc's own next bead produces -- R2605-6/AC-6 cleans
    #: scripts/ea-conformance.py AND deletes its grandfather line in one change --
    #: and a silently-orphaned entry would tolerate the path being re-shadowed
    #: later without review, defeating "cannot quietly become permanent".
    visited_debt: set[str] = set()
    checked = 0
    skipped_grandfathered = 0
    target_counts = {target.name: 0 for target in targets}

    for abs_path, rel in sorted(_iter_candidate_files(root), key=lambda item: item[1]):
        if rel in _SELF_FILES:
            continue

        matched_targets = [
            target for target, regexes in target_rules if any(r.match(rel) for r in regexes)
        ]
        for target in matched_targets:
            target_counts[target.name] += 1

        is_grandfathered = rel in grandfathered
        excluded_rule = next((rule for rule, regex in excluded_rules if regex.match(rel)), None)

        if matched_targets and (is_grandfathered or excluded_rule is not None):
            shadow_bits = []
            if is_grandfathered:
                shadow_bits.append(f"grandfathered entry in {GRANDFATHER_FILE.as_posix()}")
            if excluded_rule is not None:
                shadow_bits.append(
                    f"excluded_paths entry {excluded_rule.glob!r} ({excluded_rule.reason})"
                )
            debt = shadow_debt.get(rel)
            if debt is not None:
                visited_debt.add(rel)
            target_names = ", ".join(f"{t.name} ({t.outcome})" for t in matched_targets)
            if debt is None:
                violations.append(
                    repo_invariants.Violation(
                        f"{rel} is claimed by publication target(s) {target_names} but would "
                        f"be skipped by {'; '.join(shadow_bits)}. This shadow is undeclared: "
                        f"add {rel} to accepted_shadow_debt in {SCOPE_FILE} with a reason and "
                        "the bead that clears it, or remove the shadowing entry.",
                        pathlib.Path(rel),
                    )
                )
                continue
            if not abs_path.is_file():
                continue
            checked += 1
            try:
                content_hits = _scan_content(abs_path, patterns)
            except UnscannableFileError as exc:
                violations.append(repo_invariants.Violation(str(exc), pathlib.Path(rel)))
                continue
            if not content_hits:
                violations.append(
                    repo_invariants.Violation(
                        f"{rel} is declared in accepted_shadow_debt (cleared by "
                        f"{', '.join(debt.clears)}) as carrying a private identifier, but no "
                        "longer matches any pattern — stale entry; remove it now that the "
                        f"path is clean.",
                        pathlib.Path(rel),
                    )
                )
            continue

        if not matched_targets:
            if is_grandfathered:
                skipped_grandfathered += 1
                continue
            if excluded_rule is not None:
                continue

        if not abs_path.is_file():
            continue
        checked += 1
        try:
            content_hits = _scan_content(abs_path, patterns)
        except UnscannableFileError as exc:
            violations.append(
                repo_invariants.Violation(str(exc), pathlib.Path(rel))
            )
            continue
        for pattern, matched_text in content_hits:
            violations.append(
                repo_invariants.Violation(
                    f"{rel} matches {pattern.category} pattern {pattern.name!r} "
                    f"({matched_text!r}); a publishable artifact may not carry it. "
                    f"If this is genuinely historical or operational, add it to "
                    f"{SCOPE_FILE} with a reason; if it is pre-existing debt, add "
                    f"it to {GRANDFATHER_FILE}; otherwise remove the value.",
                    pathlib.Path(rel),
                )
            )

    for rel_path, debt in sorted(shadow_debt.items()):
        if rel_path in visited_debt:
            continue
        violations.append(
            repo_invariants.Violation(
                f"accepted_shadow_debt names {rel_path!r} (cleared by "
                f"{', '.join(debt.clears)}) but nothing shadows that path any more -- "
                "it is absent, unclaimed by a publication target, or its "
                "grandfather/excluded_paths entry is gone. The debt is discharged: "
                f"remove the entry. Leaving it would silently re-tolerate {rel_path!r} "
                "if it were shadowed again later.",
                SCOPE_FILE,
            )
        )

    for target in targets:
        if target_counts[target.name] == 0:
            violations.append(
                repo_invariants.Violation(
                    f"publication target {target.name!r} ({target.outcome}) matched zero "
                    "paths in this repository — an unmeasured target is not a clean one; "
                    "fix its glob(s) or remove the target until it has something to claim.",
                    SCOPE_FILE,
                )
            )

    summary = f"{checked} publishable path(s) checked against {len(patterns)} pattern(s)"
    if skipped_grandfathered:
        summary += f", {skipped_grandfathered} grandfathered (see {GRANDFATHER_FILE.as_posix()})"
    if targets:
        per_target = "; ".join(
            f"{target.name} ({target.outcome}): {target_counts[target.name]} scanned"
            for target in targets
        )
        summary += f"; publication targets — {per_target}"
    return repo_invariants.RuleResult(rule_id, name, summary, tuple(violations))


def _outcome_reference_problems(text: str, root: pathlib.Path) -> list[str]:
    """Every outcome reference in `text` that is bare, or that is
    release-prefixed but does not resolve against docs/releases/<release>.json."""
    problems: list[str] = []
    for match in _OUTCOME_REF_RE.finditer(text):
        release, outcome_id = match.group(1), match.group(2)
        if release is None:
            problems.append(
                f"{outcome_id!r} has no release prefix (expected e.g. 'R26.05/{outcome_id}') "
                "and therefore names no release that can be checked"
            )
            continue
        charter = root / RELEASES_DIR / f"{release}.json"
        if not charter.exists():
            problems.append(f"{release}/{outcome_id} cites release {release!r}, which has no charter at {charter.as_posix()}")
            continue
        try:
            data = json.loads(charter.read_text(encoding="utf-8"))
        except ValueError as exc:
            problems.append(f"{release}/{outcome_id} cites {charter.as_posix()}, which does not parse as JSON: {exc}")
            continue
        ids = {o.get("id") for o in data.get("outcomes", []) if isinstance(o, dict)}
        if outcome_id not in ids:
            problems.append(
                f"{release}/{outcome_id} cites an outcome that {charter.as_posix()} does not declare"
            )
    return problems


def check_publishable_scope_outcomes_are_release_prefixed_and_resolve(
    root: pathlib.Path,
) -> repo_invariants.RuleResult:
    """Every release-outcome reference in publishable-scope.yaml's declared
    reason strings is release-prefixed and resolves against its charter.

    A bare "O-5" is a violation regardless of whether some release happens
    to declare an O-5 — a bare reference names no release and therefore
    cannot be checked (R2605-4/O-7). This is what catches a renumbered
    outcome silently going stale: the referent is checked, not just the
    identifier shape.
    """
    rule_id = "publishable-scope-outcome-references"
    name = "publishable-scope.yaml outcome references are release-prefixed and resolve"

    try:
        excluded = load_excluded_paths(root)
    except ValueError as exc:
        return repo_invariants.RuleResult(
            rule_id,
            name,
            "publishable-scope declaration could not be read",
            (repo_invariants.Violation(str(exc), SCOPE_FILE),),
        )
    if excluded is None:
        return repo_invariants.RuleResult(
            rule_id, name, "skipped: no publishable-scope declaration to enforce", ()
        )

    try:
        shadow_debt = load_accepted_shadow_debt(root)
    except ValueError as exc:
        return repo_invariants.RuleResult(
            rule_id,
            name,
            "publishable-scope declaration could not be read",
            (repo_invariants.Violation(str(exc), SCOPE_FILE),),
        )

    violations: list[repo_invariants.Violation] = []
    checked = 0
    for rule in excluded:
        checked += 1
        for problem in _outcome_reference_problems(rule.reason, root):
            violations.append(
                repo_invariants.Violation(
                    f"excluded_paths entry {rule.glob!r} reason cites {problem}", SCOPE_FILE
                )
            )
    for entry_path, debt in shadow_debt.items():
        checked += 1
        for problem in _outcome_reference_problems(debt.reason, root):
            violations.append(
                repo_invariants.Violation(
                    f"accepted_shadow_debt entry {entry_path!r} reason cites {problem}", SCOPE_FILE
                )
            )

    summary = f"{checked} reason string(s) checked for outcome references"
    return repo_invariants.RuleResult(rule_id, name, summary, tuple(violations))
