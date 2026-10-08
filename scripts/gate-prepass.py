#!/usr/bin/env python3
"""Mechanical advisory pre-pass for release-gate PR checks.

The script deliberately stops at facts a machine can check. A PASS means the
pre-pass found no mechanical blocker; it is not a merge verdict.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.error
from dataclasses import dataclass
from typing import Any, Iterable, Sequence


REPO = pathlib.Path(__file__).resolve().parents[1]


def _load_shared(name: str):
    module = sys.modules.get(name)
    if module is not None:
        return module
    path = pathlib.Path(__file__).resolve().with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load shared module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_markers = _load_shared("gate_markers")
_substrate = _load_shared("substrate_client")
UUID_RE = _markers.UUID_RE
BEAD_LINE_RE = _markers.BEAD_LINE_RE
DEV_TASK_REF_RE = _markers.DEV_TASK_REF_RE
OUTER_LOOP_RE = _markers.OUTER_LOOP_RE
find_bead_id = _markers.find_bead_id
CHANGE_KIND_RE = re.compile(r"(?m)^Change kind:\s*(structural|behavioral)\s*$")
# The dispatcher's own PR-body template (apps/factory-dispatcher/dispatch.py,
# open_pull_request) opens every dispatched PR with this exact sentence,
# naming the originating dev.task bead. A body that opens this way is
# dispatcher-shaped: roughly a quarter of it (the "## Worker's own report"
# code fence, populated from worker stdout at dispatch.py:3277 /
# activities/dispatch_steps.py:1072) is machine-authored, so a dispatched
# worker can emit a line that matches OUTER_LOOP_RE itself and have it land
# inside that fence. A dispatcher-shaped body is therefore never read as
# outer-loop, no matter where an outer-loop marker appears in it -- see
# _is_body_outer_loop below, the single predicate _bead_check, _scope_check,
# and _release_check all call.
#
# Known limitation (F-1/2026-09-18): this anchors on `\A\s*` plus the literal
# opening sentence, so an ATTENDED body that merely opens by quoting the
# dispatcher's template sentence (e.g. to cite it as an example) is also
# classified dispatcher-shaped, and is then graded against the bead that
# quoted sentence names instead of treated as outer-loop. Do not open an
# attended, non-dispatched PR body with this sentence. The failure is
# closed-direction (PRIN-015): it produces a false FAIL (graded against a
# bead never dispatched from), never a false PASS, so it is left
# undocumented-as-tightened rather than risk narrowing the match away from
# the dispatcher's own live template (see the factory-template fixture).
DISPATCHER_TEMPLATE_OPENING_RE = re.compile(
    r"\A\s*Dispatched by the factory from `dev\.task`", re.IGNORECASE
)


def _is_body_outer_loop(body: str) -> bool:
    """True iff `body` carries an explicit outer-loop marker that is not
    inside a dispatcher-shaped body -- the single predicate _bead_check,
    _scope_check, and _release_check all use to decide the same question.

    A dispatcher-shaped body's "## Worker's own report" fence is worker
    stdout, interpolated verbatim (dispatch.py:3277 /
    activities/dispatch_steps.py:1072), so a dispatched worker could forge
    its own "outer-loop" escape hatch simply by emitting the marker line in
    its own report -- OUTER_LOOP_RE is a plain (?im)^...$ regex and matches
    inside that fence just as readily as anywhere else in the body. A body
    carrying both signals is therefore decided against the marker:
    dispatcher-shaped always means graded against the bead it names, never
    outer-loop, regardless of where in the body a marker appears.
    """
    dispatcher_shaped = DISPATCHER_TEMPLATE_OPENING_RE.match(body) is not None
    return not dispatcher_shaped and OUTER_LOOP_RE.search(body) is not None


# `## Dispatcher verification` is where the live dispatcher template embeds the
# verification it ran in the clone; factory PRs never carry a section literally
# titled "Verification Evidence".
VERIFICATION_EVIDENCE_RE = re.compile(
    r"(?im)^\s*(?:#{1,6}\s*)?(?:Verification[- ]Evidence\s*:?|Dispatcher verification)\s*$"
)
CHECK_ORDER = (
    "bead",
    "change-kind",
    "scope",
    "ci",
    "ci-coverage",
    "verification-evidence",
    # Advisory only (CheckResult.advisory): reported like every other check
    # but never folded into the summary or exit code. Its own contract is
    # "a pre-pass PASS is not a verdict"; a check that started blocking would
    # quietly turn the pre-pass into a second gate.
    "release",
    # Advisory for the same reason: whether the tested tree is still the
    # tree that would merge is a question this pre-pass can now ask, but an
    # unmeasured comparison (missing commits_behind/base_changed_files, e.g.
    # when the live resolver below could not run) must never be able to sink
    # the verdict.
    "freshness",
)
# WIDENED 2026-09-24 per the Operator's decision on dev.task's blocking question
# (note 4c6a1933): ".github/workflows/**" alone left ".github/test-map.yaml"
# and ".github/ci-job-classes.yaml" -- the two files that decide WHICH gates
# judge a change -- unprotected, even though "the factory may not write the
# gates that judge it" (PC-FAC-002) does not carve those two files out. The
# outer-loop escape hatch (`Outer-loop: true`, see _is_body_outer_loop above)
# is unaffected by this widening: it is checked before this tuple is ever
# consulted, so a deliberate, attended edit to any ".github/**" path still
# reaches the tree the same way the five prior ones did.
FACTORY_ALWAYS_FORBIDDEN = (".github/**",)

# OPS-147: a dated snapshot, NOT the authority. Captured 2026-09-17 from the
# top-level `name:` of every job in .github/workflows/lint.yml and
# secret-scan.yml that runs on `pull_request` (15 candidates), minus two
# excluded by judgment call rather than by reading the live branch ruleset
# (which this repo's tests may never call over the network, and which this
# script is forbidden to read from a committed copy under .github/**):
# "Detect obliged suites" (lint.yml:changes) is the R3.2 selector itself, not
# a quality gate, and its own failure already cascades into the jobs it
# gates via their `if:` conditions rather than needing a second independent
# requirement.
#
# CORRECTED AT THE #921 GATE (finding F-1, HIGH), after the outer loop read
# ruleset 16510750 and diffed it against this tuple. The first draft excluded
# "YAML syntax" by the judgment call that the k8s-manifests jobs cover the
# same manifests more specifically, and included "PR base reaches trunk".
# BOTH WERE WRONG, AND THEY CANCELLED: the ruleset requires "YAML syntax" and
# does NOT require "PR base reaches trunk", so the totals agreed at 13 while
# the MEMBERSHIP did not, and the comment's appeal to the matching count read
# as corroboration when it was coincidence. Measured:
#     required by the ruleset, absent here : "YAML syntax"
#     present here, not ruleset-required   : "PR base reaches trunk"
# The omission was the dangerous half -- a PR whose rollup lacked "YAML
# syntax" but carried the other twelve reported "all 13 required context(s)
# present", i.e. a genuinely unmeasured required suite reading as covered,
# which is the exact class this check exists to close. The staleness test
# below structurally cannot see it, because "YAML syntax" IS still a job
# name; only a diff against the ruleset finds it.
#
# "PR base reaches trunk" is KEPT deliberately: it runs on every PR today and
# is a real gate, it is simply not ruleset-required, so its absence from a
# rollup should still be noticed. That is a judgment call and is recorded as
# one -- unlike the count, it is not claimed to come from the ruleset.
#
# COST: this is exactly the proxy PRIN-015 warns about -- a copy that stops
# tracking the property it was copied from. If the ruleset adds, renames, or
# stops requiring a context, this constant goes stale silently in whichever
# direction is wrong: a name removed from the ruleset stays here and can
# make ci-coverage FAIL a fully-measured PR; a name added to the ruleset but
# never added here lets an unmeasured PR read as fully covered.
#
# NOTICED WHEN: test_default_required_contexts_are_still_real_job_names
# (scripts/tests/test_gate_prepass.py) reads the `name:` fields straight out
# of the two workflow files (a read, never a write, of .github/**) and fails
# if any entry here is no longer a job name in either file -- it catches a
# rename or removal. It CANNOT catch the ruleset changing which subset of
# still-existing job names it requires; that direction has no instrument and
# is an accepted residual risk of route (a) (see .factory/design.md).
DEFAULT_REQUIRED_CONTEXTS = (
    "Validate PR Change Kind",
    "PR base reaches trunk",
    "Repo hygiene guard",
    "Repo invariants",
    "EA model conformance",
    "K8s schema validation",
    "K8s best practices",
    "Python lint",
    "Dockerfile lint",
    "OpenAPI Contract Check",
    "Console Unit Tests",
    "Python Unit & Contract Tests",
    "Scan for secrets",
    "YAML syntax",
)


def resolve_required_contexts(explicit: str | None) -> list[str]:
    """Parse ``--required-contexts``/``GATE_PREPASS_REQUIRED_CONTEXTS`` (already
    merged by argparse's ``default=os.environ.get(...)``) if either supplied
    a value, else fall back to :data:`DEFAULT_REQUIRED_CONTEXTS` so the
    coverage check is active with no flag and no environment variable set
    (OPS-147 AC-1) instead of silently degrading to SKIP (OPS-134's original,
    now-superseded default).

    ``explicit is None`` means neither the flag nor the environment variable
    was given at all -- the case AC-1 governs. ``explicit == ""`` means one
    of them was given and was empty: a deliberate, explicit opt-out (used by
    this file's own tests that exercise ``ci``/dedup behaviour unrelated to
    coverage), not the same case, and returns ``[]`` rather than the default.
    """
    if explicit is None:
        return list(DEFAULT_REQUIRED_CONTEXTS)
    return [name.strip() for name in explicit.split(",") if name.strip()]


@dataclass(frozen=True)
class PullRequest:
    number: int | str
    body: str
    changed_files: tuple[str, ...]
    ci_payload: Any
    bead: dict[str, Any] | None = None
    # None means "not read" (no fixture data, or the substrate call failed) —
    # distinct from an empty list, which means the read succeeded and found no
    # links. Only the empty-list case can ever ground a FAIL for the release
    # check; None can only ever ground "could not run".
    bead_links: list[dict[str, Any]] | None = None
    # Both None means "not resolved" -- no fixture data, and no live resolver
    # wired up yet (see freshness check below). Only ever grounds "could not
    # run"; never guessed into a PASS or FAIL.
    commits_behind: int | None = None
    base_changed_files: frozenset[str] | None = None


@dataclass(frozen=True)
class CheckResult:
    pr: int | str
    check: str
    ok: bool
    evidence: str
    # Advisory checks never affect the summary counts or the exit code.
    advisory: bool = False
    # False means the check could not establish an answer at all (data
    # unreadable) — rendered distinctly from a real PASS/FAIL and always ok=False,
    # so an unreadable check can never be mistaken for a pass it did not earn.
    ran: bool = True


def _load_dispatcher_guards():
    dispatcher_dir = REPO / "apps" / "factory-dispatcher"
    path = dispatcher_dir / "guards.py"
    dispatcher_path = str(dispatcher_dir)
    if dispatcher_path not in sys.path:
        sys.path.insert(0, dispatcher_path)
    spec = importlib.util.spec_from_file_location("factory_dispatcher_guards", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load dispatcher guards from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _normalize_path(path: str) -> str:
    return path.strip().removeprefix("./")


def _json_load(path: pathlib.Path) -> dict[str, Any]:
    with path.open() as handle:
        return json.load(handle)


def _coerce_files(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    files: list[str] = []
    for entry in value:
        if isinstance(entry, str):
            files.append(_normalize_path(entry))
        elif isinstance(entry, dict):
            path = entry.get("path") or entry.get("filename") or entry.get("name")
            if path:
                files.append(_normalize_path(str(path)))
    return tuple(files)


def _fixture_pr_dirs(root: pathlib.Path) -> list[pathlib.Path]:
    if (root / "pr.json").exists():
        return [root]
    direct = sorted(path for path in root.iterdir() if path.is_dir() and (path / "pr.json").exists())
    if direct:
        return direct
    return sorted(path.parent for path in root.rglob("pr.json"))


def _read_fixture_pr(path: pathlib.Path) -> PullRequest:
    payload = _json_load(path / "pr.json")
    body = payload.get("body")
    if body is None and (path / "body.md").exists():
        body = (path / "body.md").read_text()
    if body is None:
        body = ""

    files = _coerce_files(payload.get("changed_files") or payload.get("files"))
    if not files and (path / "files.txt").exists():
        files = tuple(
            _normalize_path(line)
            for line in (path / "files.txt").read_text().splitlines()
            if line.strip()
        )

    bead = payload.get("bead")
    if bead is None and (path / "bead.json").exists():
        bead = _json_load(path / "bead.json")

    bead_links = payload.get("bead_links")
    if bead_links is None and (path / "links.json").exists():
        bead_links = _json_load(path / "links.json")

    commits_behind = payload.get("commits_behind")

    base_changed_files = payload.get("base_changed_files")
    if base_changed_files is not None:
        base_changed_files = frozenset(_coerce_files(base_changed_files))

    ci_payload = (
        payload.get("ci_conclusion")
        if "ci_conclusion" in payload
        else payload.get("ci", payload.get("statusCheckRollup"))
    )

    return PullRequest(
        number=payload.get("number", path.name),
        body=str(body),
        changed_files=files,
        ci_payload=ci_payload,
        bead=bead,
        bead_links=bead_links,
        commits_behind=commits_behind,
        base_changed_files=base_changed_files,
    )


def load_fixture_prs(root: pathlib.Path) -> list[PullRequest]:
    dirs = _fixture_pr_dirs(root)
    if not dirs:
        raise ValueError(f"{root} contains no pr.json fixture")
    return [_read_fixture_pr(path) for path in dirs]


def _run_gh(pr_number: str) -> dict[str, Any]:
    result = subprocess.run(
        [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--json",
            "number,body,files,statusCheckRollup,baseRefName,headRefOid",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh pr view {pr_number} failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def _resolve_freshness_via_compare(
    base_ref: str | None, head_sha: str | None
) -> tuple[int | None, frozenset[str] | None]:
    """Commits behind and base's changed files since the merge-base, read via
    ``gh api compare`` -- no local checkout, no fetch, and no mutation of a
    shared checkout (the obstacle the #884 gate named as the reason
    ``resolve_git_freshness`` never gained a production call site).

    Compares ``head_sha...base_ref`` -- reversed from the usual base...head
    reading direction -- so one call answers both questions this needs: in
    that direction, ``ahead_by`` is exactly how far ``base_ref`` has moved
    past its merge-base with ``head_sha`` (the commits-behind count from
    head's side), and ``files`` is the diff between that merge-base and
    ``base_ref`` -- exactly the files base changed since diverging.

    Degrades to ``(None, None)`` on any failure -- missing refs, a non-zero
    exit, an unreadable response -- never raising: like the release check's
    substrate calls, this is advisory-only and must never abort the pre-pass
    over one PR's freshness data.
    """
    if not base_ref or not head_sha:
        return None, None
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{{owner}}/{{repo}}/compare/{head_sha}...{base_ref}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None, None
        payload = json.loads(result.stdout)
    except (OSError, json.JSONDecodeError):
        return None, None
    behind = payload.get("ahead_by")
    files = payload.get("files")
    if not isinstance(behind, int) or not isinstance(files, list):
        return None, None
    return behind, frozenset(_coerce_files(files))


def _bead_from_dir(bead_id: str, bead_dir: pathlib.Path | None) -> dict[str, Any] | None:
    if bead_dir is None:
        return None
    for name in (f"{bead_id}.json", bead_id, "bead.json"):
        candidate = bead_dir / name
        if candidate.exists() and candidate.is_file():
            return _json_load(candidate)
    return None


def load_gh_prs(
    pr_numbers: Sequence[str],
    bead_dir: pathlib.Path | None = None,
    substrate_url: str | None = None,
    substrate_key: str | None = None,
) -> list[PullRequest]:
    client = _substrate.reader(base_url=substrate_url, key=substrate_key)
    prs: list[PullRequest] = []
    for pr_number in pr_numbers:
        payload = _run_gh(pr_number)
        body = payload.get("body") or ""
        bead_id = find_bead_id(body)
        bead = _bead_from_dir(bead_id, bead_dir) if bead_id else None
        if bead is None and bead_id and substrate_url:
            try:
                bead = client.get(f"/beads/{bead_id}")
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
                raise RuntimeError(f"could not fetch bead {bead_id}: {exc}") from exc

        # Unlike the bead fetch above, a failure here must never abort the
        # whole pre-pass: the release check is advisory, so it degrades to
        # "could not run" (bead_links stays None) rather than raising.
        bead_links: list[dict[str, Any]] | None = None
        if bead_id and substrate_url:
            try:
                links_data = client.get(f"/beads/{bead_id}/links?direction=both")
                bead_links = (
                    links_data
                    if isinstance(links_data, list)
                    else list((links_data or {}).get("items") or (links_data or {}).get("links") or [])
                )
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, RuntimeError):
                bead_links = None

        commits_behind, base_changed_files = _resolve_freshness_via_compare(
            payload.get("baseRefName"), payload.get("headRefOid")
        )

        prs.append(
            PullRequest(
                number=payload.get("number", pr_number),
                body=body,
                changed_files=_coerce_files(payload.get("files")),
                ci_payload=payload.get("statusCheckRollup"),
                bead=bead,
                bead_links=bead_links,
                commits_behind=commits_behind,
                base_changed_files=base_changed_files,
            )
        )
    return prs


def _bead_check(pr: PullRequest) -> CheckResult:
    # Same predicate _scope_check and _release_check use (see
    # _is_body_outer_loop): a bare OUTER_LOOP_RE.search here would read a
    # marker forged into a dispatcher-shaped body's worker-report fence as
    # an explicit outer-loop declaration, reporting a dispatched PR as
    # attended in the one line a human reads.
    if _is_body_outer_loop(pr.body):
        return CheckResult(pr.number, "bead", True, "PR is explicitly marked outer-loop")

    bead_id = find_bead_id(pr.body)
    if bead_id:
        return CheckResult(
            pr.number, "bead", True, f"originating dev.task bead id {bead_id} recognised"
        )

    bead_lines = BEAD_LINE_RE.findall(pr.body)
    if bead_lines:
        return CheckResult(
            pr.number,
            "bead",
            False,
            f"dispatched-bead id is not uuid-shaped: {bead_lines[0].strip('`')}",
        )

    return CheckResult(
        pr.number,
        "bead",
        False,
        "no dispatched-bead id line, no dev.task reference, and no explicit outer-loop marker",
    )


def _change_kind_check(pr: PullRequest) -> CheckResult:
    matches = CHANGE_KIND_RE.findall(pr.body)
    if len(matches) == 1:
        return CheckResult(pr.number, "change-kind", True, f"exactly one Change kind line: {matches[0]}")
    return CheckResult(
        pr.number,
        "change-kind",
        False,
        f"expected exactly one valid Change kind line, found {len(matches)}",
    )


def _scope_from_bead(bead: dict[str, Any]) -> dict[str, Any]:
    if isinstance(bead.get("scope"), dict):
        return dict(bead["scope"])
    content = bead.get("content")
    if isinstance(content, dict) and isinstance(content.get("scope"), dict):
        return dict(content["scope"])
    return {}


def _scope_check(pr: PullRequest, guards: Any) -> CheckResult:
    # An explicit outer-loop marker wins over a bead id that merely resolves
    # from the body -- DEV_TASK_REF_RE (#432) exists to recognise the
    # dispatcher's own template, and a PR that mentions a bead in passing
    # (citing a task it just filed, a predecessor, a quoted finding) matches
    # it too. That match names a bead this PR MENTIONS, not one it was
    # DISPATCHED from, so it must never supply the scope this PR is graded
    # against. This mirrors _bead_check above, which uses the same
    # _is_body_outer_loop predicate before ever looking for a bead id --
    # one answer to "is this body outer-loop", shared by every check.
    #
    # But the marker never wins over a DISPATCHER-SHAPED body -- see
    # _is_body_outer_loop's docstring for the forgery this guards against.
    outer_loop = _is_body_outer_loop(pr.body)
    if outer_loop:
        return CheckResult(pr.number, "scope", True, "outer-loop PR has no bead scope to check")
    if pr.bead is None:
        return CheckResult(
            pr.number,
            "scope",
            False,
            "bead content unavailable; cannot check changed files against scope",
        )

    scope = _scope_from_bead(pr.bead)
    if not scope:
        return CheckResult(pr.number, "scope", False, "bead content supplied no scope")

    # F-3/2026-09-18: `outer_loop` is always False here -- the `if outer_loop:`
    # return above covers every path that would make it True, so this `if not
    # outer_loop:` always takes its True branch. Left in place rather than
    # removed: proving that and deleting the conditional is a structural,
    # behavior-preserving simplification, and this function's behavior is
    # currently pinned by a behavioral bead (Tidy-First: the two kinds of
    # change do not share a commit). A future structural bead may remove it,
    # citing this comment as the proof obligation already discharged.
    if not outer_loop:
        forbidden = list(scope.get("forbidden_paths") or [])
        forbidden.extend(FACTORY_ALWAYS_FORBIDDEN)
        scope["forbidden_paths"] = forbidden

    verdict = guards.check_scope(pr.changed_files, scope)
    if verdict.ok:
        return CheckResult(
            pr.number,
            "scope",
            True,
            f"{len(pr.changed_files)} changed file(s) checked within bead scope",
        )
    return CheckResult(pr.number, "scope", False, verdict.describe())


def _is_current_failure(entry: dict[str, Any]) -> bool:
    conclusion = str(entry.get("conclusion") or entry.get("state") or "").lower()
    return conclusion not in ("success", "skipped")


def _latest_per_context(rollup: Iterable[Any]) -> list[Any]:
    """Collapse each check-run context to its most-recently-started entry.

    A context can appear more than once in the rollup when a run has been
    superseded by a newer run of the SAME context -- most commonly because
    the documented recovery from the change-kind trap is to close and reopen
    the PR, which re-runs every check without removing the stale entry. A
    stale run must never still count as a current conclusion.

    Entries are grouped by context name (`name` for a GraphQL CheckRun,
    `context` for a legacy StatusContext). Within a group, the entry with the
    latest `startedAt` wins. On an exact tie -- two runs of one context
    reporting the same start time -- the entry that reads as a FAILURE wins,
    not the success: the tie means we cannot tell which run is actually
    current, and guessing "success" is the one guess that could silently
    launder away a real failure. That would repeat this exact defect in a new
    shape, so ties resolve fail-closed instead.

    An entry with no `startedAt` at all cannot be safely ordered against its
    siblings in the same context, so it is never dropped: the whole group is
    left undeduplicated rather than risk discarding whichever entry is
    actually current. Entries with no identifiable context name cannot be
    grouped either, and are always kept.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    unkeyed: list[Any] = []
    for entry in rollup:
        if not isinstance(entry, dict):
            unkeyed.append(entry)
            continue
        name = entry.get("name") or entry.get("context")
        if not name:
            unkeyed.append(entry)
            continue
        if name not in groups:
            groups[name] = []
            order.append(name)
        groups[name].append(entry)

    result: list[Any] = []
    for name in order:
        entries = groups[name]
        if len(entries) == 1 or any(not entry.get("startedAt") for entry in entries):
            result.extend(entries)
            continue
        latest_started = max(entry["startedAt"] for entry in entries)
        candidates = [entry for entry in entries if entry["startedAt"] == latest_started]
        candidates.sort(key=_is_current_failure, reverse=True)
        result.append(candidates[0])
    result.extend(unkeyed)
    return result


def _conclusions_from_rollup(rollup: Iterable[Any]) -> list[str]:
    conclusions: list[str] = []
    for entry in _latest_per_context(rollup):
        if not isinstance(entry, dict):
            continue
        conclusion = entry.get("conclusion") or entry.get("state")
        status = entry.get("status")
        if conclusion:
            conclusions.append(str(conclusion).lower())
        elif status:
            conclusions.append(str(status).lower())
    return conclusions


def _ci_check(pr: PullRequest) -> CheckResult:
    payload = pr.ci_payload
    if isinstance(payload, str):
        conclusion = payload.lower()
        ok = conclusion == "success"
        return CheckResult(pr.number, "ci", ok, f"CI conclusion is {conclusion}")

    if isinstance(payload, dict):
        conclusion = str(payload.get("conclusion") or payload.get("state") or "").lower()
        ok = conclusion == "success"
        evidence = f"CI conclusion is {conclusion or 'missing'}"
        return CheckResult(pr.number, "ci", ok, evidence)

    if isinstance(payload, list):
        conclusions = _conclusions_from_rollup(payload)
        # R3.2: the changes-gate skips suites a PR does not oblige, so a
        # `skipped` conclusion is a deliberate non-run, not a failure. At
        # least one job must still have succeeded — an all-skipped rollup
        # (e.g. the changes job itself failing and cascading) may not pass.
        acceptable = {"success", "skipped"}
        ok = "success" in conclusions and all(
            value in acceptable for value in conclusions
        )
        evidence = "CI conclusions: " + (", ".join(conclusions) if conclusions else "none")
        return CheckResult(pr.number, "ci", ok, evidence)

    return CheckResult(pr.number, "ci", False, "CI conclusion missing")


def _rollup_entry_names(rollup: Iterable[Any]) -> set[str]:
    names: set[str] = set()
    for entry in rollup:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("context")
        if name:
            names.add(str(name))
    return names


def _ci_coverage_check(pr: PullRequest, required: Sequence[str] | None) -> CheckResult:
    """Did every required suite run at all -- a question `ci` never asks.

    `ci` (above) answers "did anything that ran fail?" and stays exactly as
    it is; this answers the separate question "did the things we require
    run?", using presence in the rollup (by name, regardless of conclusion
    or in-progress status) rather than conclusion. A rollup with a required
    context still IN_PROGRESS counts as present here -- whether it passed is
    `ci`'s question, not this one.

    An unknown or empty required set is reported as unresolved (never a
    pass): a caller that has not wired up the required-context source must
    not have that silence read as "everything ran". Fail-closed on an
    ambiguous record is the reading this repository prefers (PRIN-015,
    proposed/advisory, cited as rationale and not as compelling authority).

    A ``ci_payload`` that is not a list (the compact string-conclusion shape
    some fixtures and the ``ci`` check's own string branch accept) carries no
    named contexts at all -- there is nothing to enumerate presence against,
    which is a data-shape gap, not zero suites having run. That degrades to
    "could not run" the same way an unresolved required set does, rather than
    inventing a FAIL against every required name (OPS-147: became reachable,
    and therefore had to be decided, the moment a required set became active
    by default -- previously no caller ever combined a configured required
    set with this payload shape).
    """
    if not required:
        return CheckResult(
            pr.number,
            "ci-coverage",
            False,
            "required CI context set could not be determined",
            advisory=True,
            ran=False,
        )

    payload = pr.ci_payload
    if not isinstance(payload, list):
        return CheckResult(
            pr.number,
            "ci-coverage",
            False,
            "CI rollup unavailable (no named check-run entries); cannot verify required context coverage",
            advisory=True,
            ran=False,
        )

    present = _rollup_entry_names(payload)
    missing = [name for name in required if name not in present]
    if missing:
        return CheckResult(
            pr.number,
            "ci-coverage",
            False,
            "missing required context(s): " + ", ".join(missing),
        )
    return CheckResult(
        pr.number,
        "ci-coverage",
        True,
        f"all {len(required)} required context(s) present",
    )


def _verification_evidence_check(pr: PullRequest) -> CheckResult:
    if VERIFICATION_EVIDENCE_RE.search(pr.body):
        return CheckResult(pr.number, "verification-evidence", True, "Verification Evidence section found")
    return CheckResult(
        pr.number,
        "verification-evidence",
        False,
        "Verification Evidence section missing",
    )


def _release_check(pr: PullRequest) -> CheckResult:
    """Advisory: does this PR's bead carry a delivers edge, or a waiver?

    Never reports a pass it did not establish (REL-5/AC-6): when the bead
    itself could not be resolved, or its links could not be read from the
    substrate, this reports that the check could not run — it never guesses
    a PASS from missing data. A PR whose bead cannot be resolved at all
    already has a reporting path via the ``bead`` and ``scope`` checks; this
    mirrors their "content unavailable" phrasing rather than inventing a new
    vocabulary for the same fact.
    """
    # Same precedence as _scope_check above, via the same _is_body_outer_loop
    # predicate: an explicit outer-loop marker wins over a bead id that
    # merely resolves from the body, because that id names a bead this PR
    # mentions, not one it delivers -- but never over a dispatcher-shaped
    # body, whose worker-report section is worker stdout and can carry a
    # forged marker. See _is_body_outer_loop's docstring for the full
    # rationale, which applies identically here.
    if _is_body_outer_loop(pr.body):
        return CheckResult(
            pr.number,
            "release",
            True,
            "outer-loop PR has no bead to check for release binding",
            advisory=True,
        )

    if pr.bead is None:
        return CheckResult(
            pr.number,
            "release",
            False,
            "release check could not run: bead content unavailable",
            advisory=True,
            ran=False,
        )

    content = pr.bead.get("content")
    content = content if isinstance(content, dict) else {}
    waiver = str(content.get("release_ref_waived") or "").strip()
    if waiver:
        return CheckResult(
            pr.number,
            "release",
            True,
            f"release_ref_waived recorded: {waiver}",
            advisory=True,
        )

    if pr.bead_links is None:
        return CheckResult(
            pr.number,
            "release",
            False,
            "release check could not run: release links unreadable from the substrate",
            advisory=True,
            ran=False,
        )

    delivers = any(
        isinstance(link, dict) and str(link.get("link_type") or link.get("type") or "") == "delivers"
        for link in pr.bead_links
    )
    if delivers:
        return CheckResult(pr.number, "release", True, "delivers edge found", advisory=True)
    return CheckResult(
        pr.number,
        "release",
        False,
        "no delivers edge and no release_ref_waived recorded",
        advisory=True,
    )


def resolve_git_freshness(
    repo_root: pathlib.Path, base_ref: str, head_ref: str
) -> tuple[int | None, frozenset[str] | None]:
    """How many commits ``head_ref`` is behind ``base_ref``, and the files
    ``base_ref`` changed since their merge-base -- pure git-log arithmetic
    against refs already resolvable in ``repo_root``.

    Makes no network call itself: an unresolvable ref (not fetched locally)
    degrades to ``(None, None)`` rather than fetching or raising. Resolving a
    live PR's refs into a shared checkout is a separate, deferred decision
    (see design notes); this function only ever reads what is already there.
    """

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=str(repo_root), capture_output=True, text=True, check=False
        )

    merge_base = run("merge-base", base_ref, head_ref)
    if merge_base.returncode != 0:
        return None, None
    merge_base_sha = merge_base.stdout.strip()

    behind = run("rev-list", "--count", f"{head_ref}..{base_ref}")
    if behind.returncode != 0:
        return None, None

    diff = run("diff", "--name-only", merge_base_sha, base_ref)
    if diff.returncode != 0:
        return None, None

    behind_count = int(behind.stdout.strip())
    base_changed_files = frozenset(
        _normalize_path(line) for line in diff.stdout.splitlines() if line.strip()
    )
    return behind_count, base_changed_files


def _freshness_check(
    pr_number: int | str,
    behind: int | None,
    own_changed: frozenset[str] | None,
    base_changed: frozenset[str] | None,
) -> CheckResult:
    """Advisory: is the tested tree still the tree that would merge?

    Derived, not threshold-based: a head that is behind is not itself a
    defect (identical trees still merge clean, R860-style); the only real
    residual risk is semantic, and the cheapest measurable proxy for it is
    that the PR's own changed files overlap with what the base branch
    changed since their merge-base. ``ok=False`` iff the head is behind by
    one or more commits AND that overlap is non-empty.

    Any input not yet measured reports as "could not run" (rendered SKIP) --
    never a guessed PASS, never a guessed FAIL -- naming which input was
    missing.
    """
    missing: list[str] = []
    if behind is None:
        missing.append("commits-behind count")
    if own_changed is None:
        missing.append("PR's own changed files")
    if base_changed is None:
        missing.append("base branch's changed files since merge-base")
    if missing:
        return CheckResult(
            pr_number,
            "freshness",
            False,
            "freshness check could not run: " + ", ".join(missing) + " unavailable",
            advisory=True,
            ran=False,
        )

    assert behind is not None and own_changed is not None and base_changed is not None
    overlap = sorted(own_changed & base_changed)

    if behind > 0 and overlap:
        return CheckResult(
            pr_number,
            "freshness",
            False,
            f"head is {behind} commit(s) behind base and shares changed file(s) with "
            f"base since merge-base: {', '.join(overlap)}",
            advisory=True,
        )
    if behind > 0:
        return CheckResult(
            pr_number,
            "freshness",
            True,
            f"head is {behind} commit(s) behind base but shares no changed files with "
            "base since merge-base",
            advisory=True,
        )
    return CheckResult(
        pr_number, "freshness", True, "head is not behind base", advisory=True
    )


def evaluate_prs(
    prs: Sequence[PullRequest], required_contexts: Sequence[str] | None = None
) -> list[CheckResult]:
    guards = _load_dispatcher_guards()
    results: list[CheckResult] = []
    for pr in prs:
        checks = {
            "bead": _bead_check(pr),
            "change-kind": _change_kind_check(pr),
            "scope": _scope_check(pr, guards),
            "ci": _ci_check(pr),
            "ci-coverage": _ci_coverage_check(pr, required_contexts),
            "verification-evidence": _verification_evidence_check(pr),
            "release": _release_check(pr),
            "freshness": _freshness_check(
                pr.number,
                pr.commits_behind,
                frozenset(pr.changed_files),
                pr.base_changed_files,
            ),
        }
        results.extend(checks[name] for name in CHECK_ORDER)
    return results


def render_report(results: Sequence[CheckResult]) -> str:
    lines = [
        "Gate pre-pass report",
        "A pre-pass PASS is not a verdict; it advises the gate and merges nothing.",
        "",
    ]
    for result in results:
        if not result.ran:
            status = "SKIP"
        else:
            status = "PASS" if result.ok else "FAIL"
        lines.append(f"PR #{result.pr} {status} {result.check}: {result.evidence}")
    scored = [result for result in results if not result.advisory]
    failed = sum(1 for result in scored if not result.ok)
    passed = len(scored) - failed
    lines.extend(["", f"Summary: PASS {passed}, FAIL {failed}"])
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prs", nargs="*", help="PR numbers to fetch with gh")
    parser.add_argument("--fixture-dir", type=pathlib.Path, help="offline fixture directory")
    parser.add_argument("--bead-dir", type=pathlib.Path, help="directory of bead JSON files for gh PRs")
    parser.add_argument("--substrate-url", default=os.environ.get(_substrate.URL_ENV))
    parser.add_argument("--substrate-key", default=os.environ.get(_substrate.KEY_ENV))
    parser.add_argument(
        "--required-contexts",
        default=os.environ.get("GATE_PREPASS_REQUIRED_CONTEXTS"),
        help=(
            "comma-separated list of CI context names the ci-coverage check "
            "requires (or set GATE_PREPASS_REQUIRED_CONTEXTS); defaults to "
            "DEFAULT_REQUIRED_CONTEXTS, a dated snapshot, when neither is "
            "given; never read from a committed file in this repo"
        ),
    )
    args = parser.parse_args(argv)

    if bool(args.fixture_dir) == bool(args.prs):
        parser.error("provide either --fixture-dir or one or more PR numbers")

    required_contexts = resolve_required_contexts(args.required_contexts)

    try:
        prs = (
            load_fixture_prs(args.fixture_dir)
            if args.fixture_dir
            else load_gh_prs(args.prs, args.bead_dir, args.substrate_url, args.substrate_key)
        )
        results = evaluate_prs(prs, required_contexts)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"gate-prepass error: {exc}", file=sys.stderr)
        return 2

    print(render_report(results))
    return 1 if any(not result.ok for result in results if not result.advisory) else 0


if __name__ == "__main__":
    raise SystemExit(main())
