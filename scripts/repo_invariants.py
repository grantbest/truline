#!/usr/bin/env python3
"""Static repository invariants for config names that point at repo objects.

This checker is intentionally narrow: it reads files, parses only the shapes it
needs, and never talks to a cluster or any network service.
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import re
import shlex
import stat
import subprocess
from dataclasses import dataclass
from typing import Iterable


REPO = pathlib.Path(__file__).resolve().parent.parent

ROLLOUT_SCRIPT = pathlib.Path("scripts/rollout-status.sh")
KUBE_LINTER_CONFIG = pathlib.Path(".kube-linter.yml")
K8S_DIR = pathlib.Path("infrastructure/k8s")
WORKFLOWS_DIR = pathlib.Path(".github/workflows")
MIGRATIONS_DIR = pathlib.Path("apps/substrate/migrations/versions")
RETIRED_APPLICATION_PATHS = (
    pathlib.Path("apps/substrate-sync-vikunja"),
    pathlib.Path("infrastructure/k8s/base/substrate/substrate-sync-vikunja.yaml"),
)
# The commit from which durable Change-kind declarations are auditable. A merge
# that predates the mechanism preserving the line cannot be made to carry it, so
# the baseline is where the rule starts applying — not a list of exceptions.
#
# 437186f is the last merge that landed before the rule could take effect: the
# eleven merges of the 2026-08-06 release batch, including the rule's own, were
# squashed by the old path and carry no declaration. Blaming them would make the
# guard permanently red with no action that could clear it.
#
# Move this forward only for that reason. Moving it to silence a merge that
# COULD have carried the line defeats the guard.
TIDY_FIRST_HISTORY_BASE = "437186f62c143e978e530d054f99810d35bc492f"

#: PR merge commits admitted to have lost their durable Change-kind declaration,
#: one full sha per line with the reason recorded in the file's header. An
#: append-never ratchet, mirroring ``scripts/bug-citations-grandfathered.txt``.
#: Entries are visible in the rule's own summary line so the debt is reported
#: on every run rather than disappearing once it is excused.
CHANGE_KIND_GRANDFATHER = pathlib.Path("scripts/change-kind-grandfathered.txt")


@dataclass(frozen=True)
class DeploymentRef:
    namespace: str
    name: str

    @property
    def label(self) -> str:
        return f"{self.namespace}/{self.name}"


@dataclass(frozen=True)
class DeploymentManifest:
    namespace: str | None
    name: str
    replicas: int | None
    path: pathlib.Path

    @property
    def ref(self) -> DeploymentRef | None:
        if self.namespace is None:
            return None
        return DeploymentRef(self.namespace, self.name)


@dataclass(frozen=True)
class Violation:
    message: str
    edit_file: pathlib.Path


@dataclass(frozen=True)
class RuleResult:
    rule_id: str
    name: str
    summary: str
    violations: tuple[Violation, ...]

    @property
    def passed(self) -> bool:
        return not self.violations


@dataclass(frozen=True)
class Migration:
    path: pathlib.Path
    revision: str | None
    down_revisions: tuple[str | None, ...]


@dataclass(frozen=True)
class CommitMessage:
    sha: str
    subject: str
    body: str

    @property
    def message(self) -> str:
        return f"{self.subject}\n{self.body}"


_DEPLOYMENTS_ARRAY_RE = re.compile(r"^\s*DEPLOYMENTS=\((.*?)^\s*\)", re.MULTILINE | re.DOTALL)
_QUOTED_DEPLOYMENT_RE = re.compile(r"""["']([^"'\s]+/[^"'\s]+)["']""")
_IGNORE_PATHS_RE = re.compile(r"^(\s*)ignorePaths:\s*(?:#.*)?$")
_QUOTED_LIST_ENTRY_RE = re.compile(r"""^\s*-\s*(["'])(.*?)\1""")
_RUN_LINE_RE = re.compile(r"^(\s*)(?:-\s*)?run:\s*(.*)$")
_SCRIPT_PATH_RE = re.compile(r"(?<![A-Za-z0-9_./-])((?:\./)?scripts/[A-Za-z0-9_./-]+)")
_PR_MERGE_SUBJECT_RE = re.compile(r"(?:^Merge pull request #(?P<merge>\d+)\b|\(#(?P<squash>\d+)\)$)")
_CHANGE_KIND_RE = re.compile(r"^[ \t]*Change kind:[ \t]*(structural|behavioral)[ \t]*$")
_PYTHON_INTERPRETERS = {"python", "python3", "python3.11"}
_SHELL_INTERPRETERS = {"bash", "sh", "zsh"}


def _repo_path(root: pathlib.Path, rel: pathlib.Path) -> pathlib.Path:
    return root / rel


def _relative(path: pathlib.Path, root: pathlib.Path) -> pathlib.Path:
    try:
        return path.relative_to(root)
    except ValueError:
        return path


def _strip_yaml_comment(value: str) -> str:
    quote: str | None = None
    escaped = False
    out: list[str] = []
    for ch in value:
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            continue
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in {"'", '"'}:
            quote = ch
            out.append(ch)
            continue
        if ch == "#":
            break
        out.append(ch)
    return "".join(out).strip()


def _unquote_scalar(value: str) -> str:
    value = _strip_yaml_comment(value).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _line_key_value(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or ":" not in stripped:
        return None
    key, value = stripped.split(":", 1)
    return key.strip(), value.strip()


def _top_level_scalar(lines: list[str], key: str) -> str | None:
    for line in lines:
        if _indent(line) != 0:
            continue
        item = _line_key_value(line)
        if item and item[0] == key:
            return _unquote_scalar(item[1])
    return None


def _direct_child_scalar(lines: list[str], block_key: str, child_key: str) -> str | None:
    for index, line in enumerate(lines):
        item = _line_key_value(line)
        if not item or item[0] != block_key:
            continue
        block_indent = _indent(line)
        child_indent: int | None = None
        for child in lines[index + 1 :]:
            if not child.strip() or child.lstrip().startswith("#"):
                continue
            indent = _indent(child)
            if indent <= block_indent:
                break
            if child_indent is None:
                child_indent = indent
            if indent != child_indent:
                continue
            child_item = _line_key_value(child)
            if child_item and child_item[0] == child_key:
                return _unquote_scalar(child_item[1])
    return None


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value, 10)
    except ValueError:
        return None


def _documents(text: str) -> Iterable[list[str]]:
    current: list[str] = []
    for line in text.splitlines():
        if line.strip() == "---":
            if current:
                yield current
                current = []
            continue
        current.append(line)
    if current:
        yield current


def _same_dir_kustomize_namespace(path: pathlib.Path) -> str | None:
    for name in ("kustomization.yaml", "kustomization.yml"):
        kustomization = path.parent / name
        if not kustomization.exists():
            continue
        for line in kustomization.read_text().splitlines():
            if _indent(line) == 0:
                item = _line_key_value(line)
                if item and item[0] == "namespace":
                    return _unquote_scalar(item[1])
    return None


def parse_rollout_deployments(root: pathlib.Path) -> list[DeploymentRef]:
    path = _repo_path(root, ROLLOUT_SCRIPT)
    text = path.read_text()
    match = _DEPLOYMENTS_ARRAY_RE.search(text)
    if not match:
        return []
    refs: list[DeploymentRef] = []
    for raw in _QUOTED_DEPLOYMENT_RE.findall(match.group(1)):
        namespace, name = raw.split("/", 1)
        refs.append(DeploymentRef(namespace, name))
    return refs


def find_deployment_manifests(root: pathlib.Path) -> list[DeploymentManifest]:
    k8s_root = _repo_path(root, K8S_DIR)
    manifests: list[DeploymentManifest] = []
    if not k8s_root.exists():
        return manifests

    for path in sorted(k8s_root.rglob("*.y*ml")):
        text = path.read_text()
        for doc in _documents(text):
            if _top_level_scalar(doc, "kind") != "Deployment":
                continue
            name = _direct_child_scalar(doc, "metadata", "name")
            if not name:
                continue
            namespace = _direct_child_scalar(doc, "metadata", "namespace")
            if namespace is None:
                namespace = _same_dir_kustomize_namespace(path)
            replicas = _parse_int(_direct_child_scalar(doc, "spec", "replicas"))
            manifests.append(
                DeploymentManifest(
                    namespace=namespace,
                    name=name,
                    replicas=replicas,
                    path=_relative(path, root),
                )
            )
    return manifests


def check_rollout_deployments_exist(root: pathlib.Path) -> RuleResult:
    refs = parse_rollout_deployments(root)
    manifests = find_deployment_manifests(root)
    known = {manifest.ref for manifest in manifests if manifest.ref is not None}
    violations = tuple(
        Violation(
            f"{ref.label} is listed in {ROLLOUT_SCRIPT} but no matching Deployment "
            f"manifest exists under {K8S_DIR}",
            ROLLOUT_SCRIPT,
        )
        for ref in refs
        if ref not in known
    )
    return RuleResult(
        "RULE 1",
        "rollout deployments resolve",
        f"{len(refs)} rollout deployment entr{'y' if len(refs) == 1 else 'ies'} checked",
        violations,
    )


def check_rollout_deployments_nonzero(root: pathlib.Path) -> RuleResult:
    refs = set(parse_rollout_deployments(root))
    manifests = find_deployment_manifests(root)
    violations: list[Violation] = []
    for manifest in manifests:
        if manifest.ref in refs and manifest.replicas == 0:
            violations.append(
                Violation(
                    f"{manifest.namespace}/{manifest.name} is listed in {ROLLOUT_SCRIPT}, "
                    f"but {manifest.path} sets replicas: 0; rollout status would be a false green",
                    ROLLOUT_SCRIPT,
                )
            )
    return RuleResult(
        "RULE 2",
        "rollout deployments are not scaled to zero",
        f"{len(refs)} rollout deployment entr{'y' if len(refs) == 1 else 'ies'} checked",
        tuple(violations),
    )


def parse_kube_linter_ignore_paths(root: pathlib.Path) -> list[str]:
    path = _repo_path(root, KUBE_LINTER_CONFIG)
    lines = path.read_text().splitlines()
    entries: list[str] = []
    for index, line in enumerate(lines):
        match = _IGNORE_PATHS_RE.match(line)
        if not match:
            continue
        block_indent = len(match.group(1))
        for child in lines[index + 1 :]:
            if not child.strip() or child.lstrip().startswith("#"):
                continue
            if _indent(child) <= block_indent:
                break
            entry = _QUOTED_LIST_ENTRY_RE.match(child)
            if entry:
                entries.append(entry.group(2))
        break
    return entries


def check_kube_linter_ignore_paths(root: pathlib.Path) -> RuleResult:
    entries = parse_kube_linter_ignore_paths(root)
    violations = tuple(
        Violation(
            f"{KUBE_LINTER_CONFIG} ignorePaths entry does not exist: {entry}",
            KUBE_LINTER_CONFIG,
        )
        for entry in entries
        if not _repo_path(root, pathlib.Path(entry)).exists()
    )
    return RuleResult(
        "RULE 3",
        "kube-linter ignorePaths exist",
        f"{len(entries)} ignorePaths entr{'y' if len(entries) == 1 else 'ies'} checked",
        violations,
    )


def _extract_run_blocks(path: pathlib.Path) -> Iterable[tuple[int, str]]:
    lines = path.read_text().splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        match = _RUN_LINE_RE.match(line)
        if not match:
            index += 1
            continue
        run_indent = len(match.group(1))
        rest = match.group(2).strip()
        start_line = index + 1
        if rest in {"|", "|-", "|+", ">", ">-", ">+"}:
            block: list[str] = []
            index += 1
            while index < len(lines):
                child = lines[index]
                if child.strip() and _indent(child) <= run_indent:
                    break
                block.append(child)
                index += 1
            yield start_line, "\n".join(block)
            continue
        yield start_line, _unquote_scalar(rest)
        index += 1


def _command_before_path(tokens: list[str], path_index: int) -> str | None:
    for token in reversed(tokens[:path_index]):
        if token.startswith("-") or "=" in token:
            continue
        return token
    return None


def _script_paths_from_line(line: str) -> Iterable[tuple[str, str | None]]:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return
    try:
        tokens = shlex.split(line, comments=True, posix=True)
    except ValueError:
        tokens = re.split(r"\s+", stripped)

    for index, token in enumerate(tokens):
        for match in _SCRIPT_PATH_RE.finditer(token):
            path = match.group(1).rstrip(".,;:)]}")
            previous = _command_before_path(tokens, index)
            yield path, previous


def _requires_executable(script: str, previous_token: str | None) -> bool:
    if previous_token is None:
        return True
    command = pathlib.PurePosixPath(previous_token).name
    if command in _PYTHON_INTERPRETERS:
        return False
    if command == "pytest":
        return False
    if command in _SHELL_INTERPRETERS:
        return True
    return True


def _workflow_script_refs(root: pathlib.Path) -> Iterable[tuple[pathlib.Path, int, str, bool]]:
    workflows = _repo_path(root, WORKFLOWS_DIR)
    if not workflows.exists():
        return
    for workflow in sorted(workflows.glob("*.yml")):
        rel_workflow = _relative(workflow, root)
        for start_line, run_block in _extract_run_blocks(workflow):
            for offset, line in enumerate(run_block.splitlines()):
                for script, previous in _script_paths_from_line(line):
                    rel = script[2:] if script.startswith("./") else script
                    if rel.endswith("/") or any(part in rel for part in ("*", "$")):
                        continue
                    yield rel_workflow, start_line + offset, rel, _requires_executable(rel, previous)


def _has_executable_bit(path: pathlib.Path) -> bool:
    return bool(path.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))


def check_workflow_script_paths(root: pathlib.Path) -> RuleResult:
    refs = list(_workflow_script_refs(root))
    violations: list[Violation] = []
    for workflow, line, script, needs_executable in refs:
        path = _repo_path(root, pathlib.Path(script))
        if not path.exists():
            violations.append(
                Violation(
                    f"{workflow}:{line} invokes missing repository script {script}",
                    workflow,
                )
            )
            continue
        if path.is_dir():
            continue
        if needs_executable and not _has_executable_bit(path):
            violations.append(
                Violation(
                    f"{workflow}:{line} invokes {script}, but it is not executable",
                    pathlib.Path(script),
                )
            )
    return RuleResult(
        "RULE 4",
        "workflow-invoked scripts exist and are executable",
        f"{len(refs)} workflow script reference{'s' if len(refs) != 1 else ''} checked",
        tuple(violations),
    )


def _literal_string_or_none(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and (isinstance(node.value, str) or node.value is None):
        return node.value
    return None


def _down_revisions(node: ast.AST) -> tuple[str | None, ...]:
    if isinstance(node, ast.Constant):
        value = _literal_string_or_none(node)
        return (value,)
    if isinstance(node, (ast.Tuple, ast.List)):
        values: list[str | None] = []
        for item in node.elts:
            value = _literal_string_or_none(item)
            if value is not None:
                values.append(value)
        return tuple(values)
    return ()


def parse_migrations(root: pathlib.Path) -> list[Migration]:
    versions = _repo_path(root, MIGRATIONS_DIR)
    migrations: list[Migration] = []
    if not versions.exists():
        return migrations
    for path in sorted(versions.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        revision: str | None = None
        down_revisions: tuple[str | None, ...] = ()
        for node in tree.body:
            if isinstance(node, ast.Assign):
                targets = node.targets
                value_node = node.value
            elif isinstance(node, ast.AnnAssign):
                if node.value is None:
                    # `revision: str` with no assigned value -- a valid
                    # AnnAssign, but nothing to read.
                    continue
                targets = [node.target]
                value_node = node.value
            else:
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id == "revision":
                    revision = _literal_string_or_none(value_node)
                if isinstance(target, ast.Name) and target.id == "down_revision":
                    down_revisions = _down_revisions(value_node)
        migrations.append(Migration(_relative(path, root), revision, down_revisions))
    return migrations


def check_alembic_heads(root: pathlib.Path) -> RuleResult:
    migrations = parse_migrations(root)
    violations: list[Violation] = []

    revisions: dict[str, pathlib.Path] = {}
    for migration in migrations:
        if not migration.revision:
            violations.append(
                Violation(
                    f"{migration.path} does not declare a literal revision",
                    migration.path,
                )
            )
            continue
        if migration.revision in revisions:
            violations.append(
                Violation(
                    f"{migration.path} duplicates revision {migration.revision} "
                    f"already declared by {revisions[migration.revision]}",
                    migration.path,
                )
            )
        if len(migration.revision) > 32:
            # alembic stamps version_num into a VARCHAR(32); a longer id
            # passes every test that executes the migration's SQL directly
            # and then fails the real `alembic upgrade` at stamp time with
            # StringDataRightTruncation, rolling back the whole deploy
            # (release-gate finding on #673, demonstrated against Postgres).
            violations.append(
                Violation(
                    f"{migration.path} revision id {migration.revision!r} is "
                    f"{len(migration.revision)} characters; alembic's version "
                    "table stores at most 32 — the upgrade fails at stamp "
                    "time. Shorten the id.",
                    migration.path,
                )
            )
        revisions[migration.revision] = migration.path

    children_by_down: dict[str | None, list[Migration]] = {}
    used_down_revisions: set[str] = set()
    for migration in migrations:
        if not migration.down_revisions:
            violations.append(
                Violation(
                    f"{migration.path} does not declare a literal down_revision",
                    migration.path,
                )
            )
            continue
        for down_revision in migration.down_revisions:
            children_by_down.setdefault(down_revision, []).append(migration)
            if down_revision is not None:
                used_down_revisions.add(down_revision)

    for down_revision, children in children_by_down.items():
        if len(children) <= 1:
            continue
        child_paths = ", ".join(str(child.path) for child in children)
        label = "None" if down_revision is None else down_revision
        violations.append(
            Violation(
                f"multiple migrations share down_revision {label}: {child_paths}",
                MIGRATIONS_DIR,
            )
        )

    heads = sorted(set(revisions) - used_down_revisions)
    if len(heads) != 1:
        violations.append(
            Violation(
                f"expected exactly one Alembic head, found {len(heads)}: {', '.join(heads) or 'none'}",
                MIGRATIONS_DIR,
            )
        )

    return RuleResult(
        "RULE 5",
        "Alembic migrations have one linear head",
        f"{len(migrations)} migration file{'s' if len(migrations) != 1 else ''} checked",
        tuple(violations),
    )


def check_retired_application_paths_absent(root: pathlib.Path) -> RuleResult:
    violations = tuple(
        Violation(
            f"retired Vikunja sync path must stay absent: {path}",
            path,
        )
        for path in RETIRED_APPLICATION_PATHS
        if _repo_path(root, path).exists()
    )
    return RuleResult(
        "RULE 6",
        "retired Vikunja sync paths stay absent",
        f"{len(RETIRED_APPLICATION_PATHS)} retired path"
        f"{'s' if len(RETIRED_APPLICATION_PATHS) != 1 else ''} checked",
        violations,
    )


def _git(
    root: pathlib.Path,
    args: list[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        check=check,
        text=True,
    )


def _git_ref_exists(root: pathlib.Path, ref: str) -> bool:
    return _git(root, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], check=False).returncode == 0


def _git_baseline_observable(root: pathlib.Path, baseline: str | None) -> bool:
    """Whether the audit window genuinely spans baseline..HEAD.

    Existence is not observability: in a reused CI workspace the baseline
    OBJECT can be present from old history while the checked-out merge ref,
    fetched --depth=1, is itself a shallow boundary -- traversal from HEAD
    stops immediately, the range degenerates to one commit, and every live
    grandfather entry would read as dead. Ancestry is the test that catches
    both that shape and the plain missing-object shallow clone.
    """
    if not baseline or not _git_ref_exists(root, baseline):
        return False
    return (
        _git(root, ["merge-base", "--is-ancestor", baseline, "HEAD"], check=False).returncode
        == 0
    )


def _git_commit_range(root: pathlib.Path, baseline: str | None) -> list[str]:
    if baseline and _git_ref_exists(root, baseline):
        return [f"{baseline}..HEAD"]
    return ["HEAD", "-n", "1"]


def _is_git_work_tree(root: pathlib.Path) -> bool:
    return _git(root, ["rev-parse", "--is-inside-work-tree"], check=False).returncode == 0


def _commit_messages(root: pathlib.Path, baseline: str | None) -> list[CommitMessage]:
    if not _is_git_work_tree(root):
        raise RuntimeError("repository invariant checker requires a git work tree")

    log = _git(
        root,
        [
            "log",
            "--format=%H%x00%s%x00%B%x1e",
            *_git_commit_range(root, baseline),
            "--",
        ],
        check=False,
    )
    if log.returncode != 0:
        raise RuntimeError(log.stderr.strip() or "could not read git history")

    commits: list[CommitMessage] = []
    for raw in log.stdout.rstrip("\x1e").split("\x1e"):
        if not raw:
            continue
        parts = raw.lstrip("\n").split("\x00", 2)
        if len(parts) != 3:
            continue
        sha, subject, body = parts
        commits.append(CommitMessage(sha=sha, subject=subject, body=body))
    return commits


def _pr_number_from_subject(subject: str) -> str | None:
    match = _PR_MERGE_SUBJECT_RE.search(subject)
    if not match:
        return None
    return match.group("merge") or match.group("squash")


def _change_kind_declarations(message: str) -> list[str]:
    declarations: list[str] = []
    in_fence = False
    for line in message.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _CHANGE_KIND_RE.match(line):
            declarations.append(line.strip())
    return declarations


def _grandfathered_change_kind_shas(root: pathlib.Path) -> set[str]:
    """Commit shas admitted to have lost their declaration, and why.

    Read from :data:`CHANGE_KIND_GRANDFATHER` rather than hard-coded, so the
    admission is a reviewable file with the reason next to it instead of a
    constant nobody reads. Absent file means an empty set: this checker runs
    against fixture directories that carry no ``scripts/`` tree at all, and a
    missing ratchet must not be an error there.

    Deliberately NOT the baseline. Moving ``TIDY_FIRST_HISTORY_BASE`` forward
    would silence these four commits by also blinding the rule to the forty-one
    compliant merges behind them, and its own comment forbids exactly that use.
    Naming the shas keeps the audit total and keeps the failure legible.
    """
    path = _repo_path(root, CHANGE_KIND_GRANDFATHER)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()
    shas: set[str] = set()
    for line in text.splitlines():
        entry = line.split("#", 1)[0].strip()
        if entry:
            shas.add(entry)
    return shas


def check_pr_merge_commit_change_kind(
    root: pathlib.Path,
    baseline: str | None = TIDY_FIRST_HISTORY_BASE,
) -> RuleResult:
    """Require durable Tidy-First declarations in PR merge commit messages.

    Choice: preserve the declaration into the squash/merge commit message and
    audit local git history. The rejected alternative was reading PR bodies
    through the GitHub API: that would keep the PR-time source of truth, but it
    would make this static invariant depend on network access and credentials.

    A guard that cannot observe must say so rather than accuse. This checker is
    also run against fixture directories that carry no git history at all, and
    reporting a violation there manufactures a finding about commits that do not
    exist. When this rule first shipped it did exactly that, taking an unrelated
    rule's meta-test down with it and turning `main` red.

    The ratchet also cuts both ways, matching the requirement-citations rule's
    grandfather list: a sha in :data:`CHANGE_KIND_GRANDFATHER` that matches no
    commit still needing the exemption — unreachable in the audited range, or
    one whose declaration is already intact — is dead weight nothing else
    would ever report. That entry fails the check by name instead.
    """
    if not _is_git_work_tree(root):
        return RuleResult(
            "RULE 7",
            "PR merge commits preserve Change kind",
            "skipped: not a git work tree, so there is no merge history to audit",
            (),
        )

    try:
        commits = _commit_messages(root, baseline)
    except RuntimeError as exc:
        return RuleResult(
            "RULE 7",
            "PR merge commits preserve Change kind",
            "git history could not be read",
            (Violation(str(exc), pathlib.Path("scripts/check-repo-invariants.py")),),
        )

    grandfathered_shas = _grandfathered_change_kind_shas(root)
    matched_shas: set[str] = set()
    checked = 0
    grandfathered = 0
    violations: list[Violation] = []
    for commit in commits:
        pr_number = _pr_number_from_subject(commit.subject)
        if pr_number is None:
            continue
        checked += 1
        declarations = _change_kind_declarations(commit.message)
        if len(declarations) == 1:
            continue
        if commit.sha in grandfathered_shas:
            grandfathered += 1
            matched_shas.add(commit.sha)
            continue
        short_sha = commit.sha[:8]
        violations.append(
            Violation(
                f"{short_sha} (PR #{pr_number}) must preserve exactly one bare "
                f"'Change kind: structural' or 'Change kind: behavioral' line in "
                f"the durable merge commit message; found {len(declarations)}",
                pathlib.Path("scripts/check-repo-invariants.py"),
            )
        )

    # A grandfather entry is a standing hole a future violation can hide in.
    # One that matches no commit still needing the exemption — unreachable in
    # the audited range, or already carrying exactly one declaration — is
    # dead weight nothing else would ever report; fail on it by name. But a
    # guard that cannot observe must say so rather than accuse: with the
    # baseline unreachable (a shallow clone), `_git_commit_range` degrades to
    # HEAD alone and every live entry would read as dead. Skip the staleness
    # pass there and say so in the summary instead.
    baseline_reachable = _git_baseline_observable(root, baseline)
    dead_shas = sorted(grandfathered_shas - matched_shas) if baseline_reachable else []
    for sha in dead_shas:
        violations.append(
            Violation(
                f"{sha} is grandfathered in {CHANGE_KIND_GRANDFATHER.as_posix()} but "
                "matches no PR merge commit that still needs the exemption; delete "
                "this entry.",
                CHANGE_KIND_GRANDFATHER,
            )
        )

    summary = f"{checked} PR merge commit{'s' if checked != 1 else ''} checked"
    if grandfathered:
        summary += (
            f", {grandfathered} grandfathered "
            f"(see {CHANGE_KIND_GRANDFATHER.as_posix()})"
        )
    if dead_shas:
        summary += f", {len(dead_shas)} grandfathered entry(ies) dead"
    if grandfathered_shas and not baseline_reachable:
        summary += ", grandfather staleness not audited (baseline unreachable)"
    return RuleResult(
        "RULE 7",
        "PR merge commits preserve Change kind",
        summary,
        tuple(violations),
    )


def check_personas_are_repository_artefacts(root: pathlib.Path) -> RuleResult:
    """Amendment 30 diff item 4: personas live tracked under docs/agents/.

    Two halves: every persona scripts/materialize_agents.py references exists
    under docs/agents/, and no .claude/ content is tracked - the materialised
    copies must never become a second source of truth (the ~/.gemini/GEMINI.md
    failure shape).
    """
    violations: list[Violation] = []
    materializer = root / "scripts" / "materialize_agents.py"
    personas: list[str] = []
    if not materializer.is_file():
        # House precedent (Rule 7): a rule whose subject is absent skips
        # rather than accuses - fixture repos and partial checkouts are not
        # violations. Deleting the materializer for real is caught by the
        # dispatcher's own persona-materialisation call path.
        return RuleResult(
            "personas-are-artefacts",
            "personas are repository artefacts",
            "skipped: no materializer to audit",
            (),
        )
    if True:
        match = re.search(r"PERSONAS\s*=\s*\(([^)]*)\)", materializer.read_text())
        if not match:
            violations.append(
                Violation("PERSONAS tuple not found in materializer", str(materializer))
            )
        else:
            personas = re.findall(r'"([^"]+)"', match.group(1))
            for name in personas:
                if not (root / "docs" / "agents" / name).is_file():
                    violations.append(
                        Violation(
                            "persona referenced by the materializer does not exist",
                            f"docs/agents/{name}",
                        )
                    )
    # Scoped to .claude/agents/, not .claude/: the rule's subject is
    # materialized personas (docs/agents/ is source; a persona only in
    # .claude is memory). Sibling subdirectories may be tracked on
    # purpose — .claude/skills/ is gate-reviewed harness configuration
    # (PR #425) — and a parent-wide ls-files would accuse them.
    tracked = subprocess.run(
        ["git", "-C", str(root), "ls-files", ".claude/agents/"],
        capture_output=True,
        text=True,
        check=False,
    )
    for line in tracked.stdout.splitlines():
        if line.strip():
            violations.append(
                Violation(".claude/agents/ content must never be tracked", line.strip())
            )
    return RuleResult(
        "personas-are-artefacts",
        "personas are repository artefacts",
        f"{len(personas)} persona(s) checked; .claude/agents/ tracking asserted empty",
        tuple(violations),
    )


def run_checks(root: pathlib.Path = REPO) -> list[RuleResult]:
    root = root.resolve()
    return [
        check_rollout_deployments_exist(root),
        check_rollout_deployments_nonzero(root),
        check_kube_linter_ignore_paths(root),
        check_workflow_script_paths(root),
        check_alembic_heads(root),
        check_retired_application_paths_absent(root),
        check_pr_merge_commit_change_kind(root),
        check_personas_are_repository_artefacts(root),
    ]


def format_results(results: Iterable[RuleResult]) -> str:
    lines: list[str] = []
    total_violations = 0
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        lines.append(f"{result.rule_id}: {result.name}: {status} ({result.summary})")
        if result.violations:
            total_violations += len(result.violations)
            for violation in result.violations:
                lines.append(f"  - {violation.message}")
                lines.append(f"    Remediation: edit {violation.edit_file}.")
    if total_violations:
        lines.append("")
        lines.append(f"Repository invariants failed: {total_violations} violation(s).")
    else:
        lines.append("")
        lines.append("All repository invariants passed.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        type=pathlib.Path,
        default=REPO,
        help="repository root to check (default: current checkout)",
    )
    args = parser.parse_args(argv)

    results = run_checks(args.repo)
    print(format_results(results))
    return 1 if any(result.violations for result in results) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
