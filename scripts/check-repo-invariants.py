#!/usr/bin/env python3
"""CLI wrapper for repository invariant checks."""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
import pathlib
import re
import shlex
import sys
import tomllib
from collections.abc import Iterable

import private_identifiers
import readme_conformance
import repo_invariants
import requirement_citations
import traceability
import unknown


REPO = pathlib.Path(__file__).resolve().parent.parent
LINT_WORKFLOW = pathlib.Path(".github/workflows/lint.yml")
DEV_TASKS_DIR = pathlib.Path("apps/factory-dispatcher/tasks")
REQUIREMENTS_DIR = pathlib.Path("docs/requirements")
_PACKAGE_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9_.-]*)")
_NORMALIZE_RE = re.compile(r"[-_.]+")

#: Directory names pruned while walking the tree for test modules: vendored
#: or generated content that is never part of a pytest invocation a clone
#: would actually run.
_TEST_SCAN_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        # Agent worktrees are sibling clones of this repository. Walking into
        # them accuses every test file in the main tree of colliding with its
        # own copy — 114 violations observed 2026-08-30 — and no pytest
        # invocation a clone would run ever collects them.
        ".claude",
        "venv",
        ".venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".hypothesis",
    }
)


def _canonical_package_name(spec: str) -> str | None:
    match = _PACKAGE_NAME_RE.match(spec)
    if not match:
        return None
    return _NORMALIZE_RE.sub("-", match.group(1)).lower()


def _is_install_command(tokens: list[str]) -> int | None:
    if len(tokens) >= 3 and pathlib.PurePosixPath(tokens[0]).name == "uv":
        if tokens[1:3] == ["pip", "install"]:
            return 3
    if len(tokens) >= 2 and pathlib.PurePosixPath(tokens[0]).name == "pip":
        if tokens[1] == "install":
            return 2
    return None


def _app_path_from_token(token: str) -> pathlib.Path | None:
    path = pathlib.PurePosixPath(token.rstrip("/"))
    parts = path.parts
    if "apps" not in parts:
        return None
    index = parts.index("apps")
    if len(parts) <= index + 1:
        return None
    return pathlib.Path("apps") / parts[index + 1]


def _iter_install_specs(tokens: list[str], start: int) -> Iterable[str]:
    index = start
    options_with_values = {"-r", "--requirement", "-e", "--editable", "-c", "--constraint"}
    while index < len(tokens):
        token = tokens[index]
        if token in options_with_values:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        if "/" in token or token.startswith((".", "$")):
            index += 1
            continue
        yield token
        index += 1


def _editable_app_path(tokens: list[str], start: int) -> pathlib.Path | None:
    for index, token in enumerate(tokens[start:], start=start):
        if token in {"-e", "--editable"} and index + 1 < len(tokens):
            return _app_path_from_token(tokens[index + 1])
        if token.startswith("--editable="):
            return _app_path_from_token(token.split("=", 1)[1])
    return None


def _pyproject_dependency_names(pyproject: pathlib.Path) -> tuple[set[str], set[str]]:
    data = tomllib.loads(pyproject.read_text())
    project = data.get("project") or {}
    runtime = {
        name
        for spec in project.get("dependencies") or []
        if isinstance(spec, str)
        for name in [_canonical_package_name(spec)]
        if name
    }
    optional = project.get("optional-dependencies") or {}
    test_specs = optional.get("test") if isinstance(optional, dict) else None
    test = {
        name
        for spec in test_specs or []
        if isinstance(spec, str)
        for name in [_canonical_package_name(spec)]
        if name
    }
    return runtime, test


def _pyproject_dependency_index(root: pathlib.Path) -> dict[pathlib.Path, set[str]]:
    index: dict[pathlib.Path, set[str]] = {}
    for pyproject in sorted((root / "apps").glob("*/pyproject.toml")):
        runtime, test = _pyproject_dependency_names(pyproject)
        index[pyproject.parent.relative_to(root)] = runtime | test
    return index


def check_lint_workflow_python_test_dependencies(root: pathlib.Path) -> repo_invariants.RuleResult:
    root = root.resolve()
    workflow = root / LINT_WORKFLOW
    if not workflow.exists():
        return repo_invariants.RuleResult(
            "RULE 7",
            "lint workflow test dependencies are declared",
            "0 workflow package install(s) checked",
            (),
        )

    declared = _pyproject_dependency_index(root)
    violations: list[repo_invariants.Violation] = []
    checked = 0
    for _start_line, run_block in repo_invariants._extract_run_blocks(workflow):
        active_app: pathlib.Path | None = None
        for line in run_block.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                tokens = shlex.split(stripped, comments=True, posix=True)
            except ValueError:
                continue
            install_start = _is_install_command(tokens)
            if install_start is None:
                continue
            editable_app = _editable_app_path(tokens, install_start)
            if editable_app is not None:
                active_app = editable_app
            if active_app is None or active_app not in declared:
                continue
            for spec in _iter_install_specs(tokens, install_start):
                package = _canonical_package_name(spec)
                if package is None:
                    continue
                checked += 1
                if package not in declared[active_app]:
                    violations.append(
                        repo_invariants.Violation(
                            f"{LINT_WORKFLOW} installs {package} for {active_app}, "
                            "but that package is not declared in the app's runtime "
                            "dependencies or test extra",
                            active_app / "pyproject.toml",
                        )
                    )
    return repo_invariants.RuleResult(
        "RULE 7",
        "lint workflow test dependencies are declared",
        f"{checked} workflow package install{'s' if checked != 1 else ''} checked",
        tuple(violations),
    )


def _load_ci_changes():
    """The glob matcher lives in scripts/ci_changes.py and ONLY there — a
    second implementation of `**`/`*` semantics is the drift that made the
    gate scripts unable to read the dispatcher's own PR bodies (#431/#432)."""
    path = pathlib.Path(__file__).resolve().with_name("ci_changes.py")
    spec = importlib.util.spec_from_file_location("ci_changes", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: Filename suffixes that mark a colocated JS/TS test file — "src/foo.test.tsx"
#: sitting next to "foo.tsx", not inside a tests/ directory. A matcher that only
#: looked inside tests/ directories reported lifeops-console's 14 colocated
#: tests as zero (dev.finding 87bc51c0's corrected evidence); this is why
#: _is_app_test_filename checks suffixes anywhere under the app, not a
#: directory shape.
_COLOCATED_TEST_SUFFIXES = (
    ".test.ts",
    ".test.tsx",
    ".test.js",
    ".test.jsx",
    ".spec.ts",
    ".spec.tsx",
    ".spec.js",
    ".spec.jsx",
)


def _is_app_test_filename(filename: str) -> bool:
    if filename.startswith("test_") and filename.endswith(".py"):
        return True
    if filename.endswith("_test.py"):
        return True
    return filename.endswith(_COLOCATED_TEST_SUFFIXES)


def _count_app_test_files(app_dir: pathlib.Path) -> int:
    """How many of the app's own files pytest/Jest-shaped tooling would
    collect as a test — colocated or under a tests/ directory, either way."""
    count = 0
    for dirpath, dirnames, filenames in os.walk(app_dir):
        dirnames[:] = [d for d in dirnames if d not in _TEST_SCAN_EXCLUDED_DIRS]
        for filename in filenames:
            if _is_app_test_filename(filename):
                count += 1
    return count


def check_test_map_covers_apps(root: pathlib.Path) -> repo_invariants.RuleResult:
    """R3.5: every apps/ directory that carries a test is obliged to a suite
    that can actually tell it apart from its siblings.

    An app existing outside .github/test-map.yaml is the defect that let the
    substrate run with zero CI coverage until 2026-07-27 and the automations
    suite run in no job until 2026-08-16. The probe path stands in for any
    file inside the app, using the same matcher the changes-gate runs.

    Two refinements on top of "some suite's paths match this app", both
    landed for dev.finding 87bc51c0 (OPS-143): python-quality declares
    ``paths: [apps/**, ...]`` for a LINT job, so every possible app probe
    matched it and "this app is obliged by some suite" was satisfied
    unconditionally by a suite that runs no tests — the predicate could not
    fail. apps/pg-backup's 502-line, 14-test suite ran in no CI job the whole
    time this rule reported clean.

    * A suite path pattern that matches every app in the tree is asked to
      testify for none of them — it cannot discriminate between "this app"
      and "any app", so a match against it alone is not coverage. Computed
      against the apps actually present, not against the pattern's literal
      shape, so it still fires in a tree with only one app (AC-2's fixture
      3): the question is never "does this look like a wildcard" but "does
      it distinguish this app from its siblings here".
    * An app with zero test files of its own — measured by walking the app's
      tree for pytest's and Jest/Vitest's filename conventions, colocated
      tests included, never a hardcoded allowlist — is exempted rather than
      violating: there is nothing for a suite to run, so "obliged to a
      suite" cannot be the missing property. finance-reporting (3 tracked
      files, no tests) is exempt this way; the moment it gains a first test
      file the exemption stops applying by itself.

    Tolerates a root without the map (synthetic test trees); the companion
    test pins that the live repository actually carries it.
    """
    map_path = root / ".github" / "test-map.yaml"
    apps_dir = root / "apps"
    if not map_path.exists() or not apps_dir.is_dir():
        return repo_invariants.RuleResult(
            "test-map-apps",
            "test map covers every app",
            "test map or apps/ not present; nothing to check",
            (),
        )

    import yaml

    test_map = yaml.safe_load(map_path.read_text()) or {}
    matcher = _load_ci_changes()
    named_patterns: list[tuple[str, re.Pattern[str]]] = [
        (suite_name, matcher.glob_to_regex(path))
        for suite_name, spec in (test_map.get("suites") or {}).items()
        for path in (spec.get("paths") or [])
    ]

    apps = sorted(p for p in apps_dir.iterdir() if p.is_dir())
    app_names = [app.name for app in apps]

    def matches_probe(pattern: re.Pattern[str], app_name: str) -> bool:
        return bool(pattern.match(f"apps/{app_name}/__probe__.py"))

    # A pattern that matches every app present cannot discriminate between
    # them, so a match against only such patterns testifies for no one app.
    discriminating = {
        (suite_name, pattern)
        for suite_name, pattern in named_patterns
        if not all(matches_probe(pattern, name) for name in app_names)
    }

    violations = []
    checked = 0
    exempt: list[str] = []
    for app in apps:
        checked += 1
        probe = f"apps/{app.name}/__probe__.py"
        matching = [
            (suite_name, pattern)
            for suite_name, pattern in named_patterns
            if pattern.match(probe)
        ]
        covered = any(entry in discriminating for entry in matching)
        if covered:
            continue
        if _count_app_test_files(app) == 0:
            exempt.append(app.name)
            continue
        if not matching:
            violations.append(
                repo_invariants.Violation(
                    f"apps/{app.name} matches no suite in .github/test-map.yaml — "
                    "an unmapped app ships untested the moment the changes-gate "
                    "skips suites; add it to a suite's paths",
                    map_path,
                )
            )
        else:
            over_matching = sorted({suite_name for suite_name, _ in matching})
            violations.append(
                repo_invariants.Violation(
                    f"apps/{app.name} is matched only by suite(s) "
                    f"{', '.join(over_matching)} in .github/test-map.yaml, and "
                    f"{'each of their' if len(over_matching) > 1 else 'its'} "
                    "path pattern matches every app in the tree — a pattern "
                    "that does not discriminate between apps is not evidence "
                    "of coverage for any one of them; add a suite (or narrow "
                    f"an existing one) specific enough to actually cover "
                    f"apps/{app.name}",
                    map_path,
                )
            )

    summary = (
        f"{checked} app director{'ies' if checked != 1 else 'y'} checked against the suite map"
    )
    if exempt:
        exempt_label = ", ".join(f"apps/{name}" for name in exempt)
        summary += f"; {len(exempt)} exempt (no test files): {exempt_label}"
    return repo_invariants.RuleResult(
        "test-map-apps",
        "test map covers every app",
        summary,
        tuple(violations),
    )


def check_job_classes_complete(root: pathlib.Path) -> repo_invariants.RuleResult:
    """R2.1 enforcement: workflows and .github/ci-job-classes.yaml agree.

    Every workflow job is classified exactly once (hermetic or host-bound),
    and the registry names no job that no longer exists — a name in config
    outliving the thing it named is this repository's documented failure
    mode, and it is how the offsite backup died on a decommissioned
    database for 18 days.
    """
    classes_path = root / ".github" / "ci-job-classes.yaml"
    workflows_dir = root / ".github" / "workflows"
    if not classes_path.exists() or not workflows_dir.is_dir():
        return repo_invariants.RuleResult(
            "ci-job-classes",
            "CI job classes registry is complete",
            "registry or workflows not present; nothing to check",
            (),
        )

    import yaml

    classes = yaml.safe_load(classes_path.read_text()) or {}
    hermetic = {
        (wf, job)
        for wf, jobs in (classes.get("hermetic") or {}).items()
        for job in (jobs or [])
    }
    host_bound = {
        (wf, job)
        for wf, jobs in (classes.get("host-bound") or {}).items()
        for job in (jobs or {})
    }

    actual = set()
    for workflow in sorted(workflows_dir.glob("*.yml")) + sorted(workflows_dir.glob("*.yaml")):
        data = yaml.safe_load(workflow.read_text()) or {}
        for job in (data.get("jobs") or {}):
            actual.add((workflow.name, str(job)))

    violations = []
    for wf, job in sorted(actual - (hermetic | host_bound)):
        violations.append(
            repo_invariants.Violation(
                f"{wf}:{job} is not classified in .github/ci-job-classes.yaml — "
                "every job is hermetic or host-bound; the break-glass flow "
                "moves exactly the hermetic set, so an unclassified job is "
                "unreachable in an outage",
                classes_path,
            )
        )
    for wf, job in sorted((hermetic | host_bound) - actual):
        violations.append(
            repo_invariants.Violation(
                f"{wf}:{job} is registered in ci-job-classes.yaml but exists in "
                "no workflow — a name outliving the thing it named",
                classes_path,
            )
        )
    for wf, job in sorted(hermetic & host_bound):
        violations.append(
            repo_invariants.Violation(
                f"{wf}:{job} is classified both hermetic and host-bound",
                classes_path,
            )
        )
    return repo_invariants.RuleResult(
        "ci-job-classes",
        "CI job classes registry is complete",
        f"{len(actual)} workflow job(s) reconciled against the registry",
        tuple(violations),
    )


def _iter_test_module_files(root: pathlib.Path) -> Iterable[pathlib.Path]:
    """Every file pytest's default collection globs would pick up.

    ``test_*.py`` and ``*_test.py`` are pytest's ``python_files`` defaults;
    nothing in this repo overrides them (checked: no ``pytest.ini``,
    ``pyproject.toml [tool.pytest.ini_options]``, or ``setup.cfg`` sets
    ``python_files``).
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _TEST_SCAN_EXCLUDED_DIRS]
        for filename in filenames:
            if filename.startswith("test_") and filename.endswith(".py"):
                yield pathlib.Path(dirpath) / filename
            elif filename.endswith("_test.py"):
                yield pathlib.Path(dirpath) / filename


def _pytest_module_key(path: pathlib.Path) -> tuple[str, ...]:
    """The module identity pytest's default "prepend" import mode derives.

    Walk up from the file while its ancestor directories carry
    ``__init__.py``; stop at the first one that doesn't. A directory with no
    ``__init__.py`` — every test directory in this repo, today — yields a
    single-element key: the bare basename, which is exactly how two
    unrelated ``test_release_status.py`` files produced the same
    ``sys.modules`` entry and made the second one collected a hard
    collection error (OPS-39). A file under a real package is keyed by its
    full dotted path instead, so it cannot collide with a same-named file
    that carries no distinguishing package.
    """
    parts = [path.stem]
    directory = path.parent
    while (directory / "__init__.py").is_file():
        parts.insert(0, directory.name)
        directory = directory.parent
    return tuple(parts)


def check_no_colliding_test_module_basenames(root: pathlib.Path) -> repo_invariants.RuleResult:
    """No two test modules a single pytest invocation could collect share a
    module identity.

    OPS-39 renamed the one pair that had already collided
    (``apps/factory-dispatcher/tests/test_release_status.py`` and
    ``scripts/tests/test_release_status.py``) but fixed nothing structural:
    test directories here carry no ``__init__.py``, so pytest derives a
    module name from the basename alone, and any two files anywhere in the
    tree that reach for the same obvious name reproduce the same failure.
    ``.github/workflows/lint.yml`` runs suites as separate jobs, so a fresh
    collision passes CI exactly as this one did — it is only ever found by
    whoever next runs two suites in one pytest invocation, which is what a
    clone-local verification does.

    Derived by scanning, not from a list of names already known to collide:
    the point is to catch a pair nobody has thought of yet.
    """
    by_key: dict[tuple[str, ...], list[pathlib.Path]] = {}
    checked = 0
    for path in _iter_test_module_files(root):
        checked += 1
        by_key.setdefault(_pytest_module_key(path), []).append(path)

    violations: list[repo_invariants.Violation] = []
    for key in sorted(by_key):
        paths = by_key[key]
        if len(paths) < 2:
            continue
        rels = sorted(repo_invariants._relative(p, root) for p in paths)
        message = (
            f"{rels[0]} and {rels[1]} share the test module basename {key[-1]!r} "
            "with no distinguishing package (no __init__.py separates them); a "
            "single pytest invocation collecting both would hit pytest's 'import "
            "file mismatch' collection error on the second one. The basenames must "
            "differ."
        )
        if len(rels) > 2:
            message += f" Also collides: {', '.join(str(r) for r in rels[2:])}."
        violations.append(repo_invariants.Violation(message, rels[1]))

    return repo_invariants.RuleResult(
        "no-colliding-test-basenames",
        "test modules collectible by one pytest invocation have unique basenames",
        f"{checked} test module file{'s' if checked != 1 else ''} checked",
        tuple(violations),
    )


# PC-DEL-001 / G1b: images built by these six build-*.yml workflows and
# pushed to zot. Any other image under infrastructure/k8s/ (third-party,
# already version-pinned) is out of this rule's scope.
PLATFORM_APP_IMAGE_NAMES = frozenset(
    {
        "substrate",
        "mcp-hub",
        "automations-worker",
        "lifeops-console",
        "finance-reporting",
        "pg-backup",
    }
)

_DIGEST_IMAGE_RE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_TAGGED_IMAGE_RE = re.compile(r"^[A-Za-z0-9._-]+:[A-Za-z0-9._-]+$")

#: A digest whose hex is a single repeated character names no image that can
#: exist. The all-zeros form is the placeholder #476 committed for six apps,
#: expecting an operator to fill it before the manifest synced; in a GitOps
#: repository there is no "before it syncs", so ArgoCD applied it and every
#: rollout wedged in ImagePullBackOff for two days (2026-08-21 to 08-23),
#: taking the pg-backup and pg-restore-drill CronJobs -- which have no prior
#: pod to keep serving -- down with them. The old rule accepted this: it
#: matched the digest FORM and never asked whether the digest could resolve.
_PLACEHOLDER_DIGEST_RE = re.compile(r"^[^\s@]+@sha256:([0-9a-f])\1{63}$")


def _image_app_name(image: str) -> str:
    """The bare app name from an image ref, host:port-safe.

    A registry host like ``203.0.113.10:30500`` contains a colon before
    the first ``/`` — splitting on ``:`` before isolating the last path
    segment would mis-parse the host's port as a tag.
    """
    last_segment = image.rsplit("/", 1)[-1]
    for sep in ("@", ":"):
        if sep in last_segment:
            return last_segment.split(sep, 1)[0]
    return last_segment


def _pod_spec(doc: dict) -> dict:
    kind = doc.get("kind")
    spec = doc.get("spec") or {}
    if not isinstance(spec, dict):
        return {}
    if kind == "CronJob":
        job_spec = spec.get("jobTemplate") or {}
        job_spec = job_spec.get("spec") if isinstance(job_spec, dict) else None
        template = job_spec.get("template") if isinstance(job_spec, dict) else None
    elif kind == "Pod":
        return spec
    else:
        template = spec.get("template")
    if not isinstance(template, dict):
        return {}
    pod_spec = template.get("spec")
    return pod_spec if isinstance(pod_spec, dict) else {}


def _iter_containers(doc: dict):
    pod_spec = _pod_spec(doc)
    containers = list(pod_spec.get("containers") or [])
    containers += list(pod_spec.get("initContainers") or [])
    return [c for c in containers if isinstance(c, dict)]


def check_platform_app_images_are_pinned(root: pathlib.Path) -> repo_invariants.RuleResult:
    """PC-DEL-001 / G1b: platform-app images name something the cluster can obtain.

    Two deploy models are legal, and each has one coherent pull policy:

    * ``<app>:<tag>`` with ``imagePullPolicy: Never`` — the model the platform
      actually runs today. build-*.yml builds the image and imports it into
      the node's containerd; nothing is pulled, so nothing needs a registry.
    * ``<registry>/<app>@sha256:<64 hex>`` with ``imagePullPolicy: IfNotPresent``
      — the content-addressed model G1b wants, usable once G1a's push to zot
      is enabled (``vars.ZOT_PUSH_ENABLED``) and the image is actually there.

    What is never legal is a digest that cannot resolve. #476 pinned six apps
    to ``@sha256:000...0`` as a placeholder, with a comment saying to fill it
    "before this manifest may sync" — but this is a GitOps repository, where
    merge IS deploy: ArgoCD applied the placeholders, every rollout wedged in
    ImagePullBackOff, and the platform was undeployable from 2026-08-21 to
    2026-08-23 while old pods kept serving and nothing reported it. The
    pg-backup and pg-restore-drill CronJobs, which have no prior pod to fall
    back on, simply failed: the 2026-08-23 backup and restore drill both died
    on the unpullable digest.

    The rule this replaces asked only whether an image was digest-SHAPED. It
    passed on the placeholder that broke the platform, and it would have
    failed the revert that fixed it — an invariant enforcing an aspiration
    rather than the deploy path the platform has. This one asks whether the
    reference could ever resolve, and lets each app move to digests when its
    registry half is real, not before.
    """
    import yaml

    k8s_root = root / "infrastructure" / "k8s"
    violations: list[repo_invariants.Violation] = []
    checked = 0

    if k8s_root.is_dir():
        for path in sorted(k8s_root.rglob("*.y*ml")):
            rel = path.relative_to(root)
            if "secrets" in rel.parts:
                continue
            try:
                docs = list(yaml.safe_load_all(path.read_text()))
            except yaml.YAMLError:
                continue
            for doc in docs:
                if not isinstance(doc, dict):
                    continue
                for container in _iter_containers(doc):
                    image = str(container.get("image") or "")
                    if _image_app_name(image) not in PLATFORM_APP_IMAGE_NAMES:
                        continue
                    checked += 1
                    label = f"{rel}: container {container.get('name', '<unnamed>')} (image={image!r})"
                    pull_policy = container.get("imagePullPolicy")
                    if _PLACEHOLDER_DIGEST_RE.match(image):
                        violations.append(
                            repo_invariants.Violation(
                                f"{label} pins a placeholder digest that can never resolve. "
                                "A manifest in this repository is applied by ArgoCD as soon as "
                                "it merges; there is no window in which an unresolvable digest "
                                "is safe. Resolve it with scripts/pin-image-digest.sh, or use "
                                "the <app>:<tag> + imagePullPolicy: Never model until the "
                                "registry push (G1a) is enabled.",
                                rel,
                            )
                        )
                    elif _DIGEST_IMAGE_RE.match(image):
                        if pull_policy != "IfNotPresent":
                            violations.append(
                                repo_invariants.Violation(
                                    f"{label} pins a digest but sets imagePullPolicy="
                                    f"{pull_policy!r}; a digest is pulled, so it must be "
                                    "IfNotPresent",
                                    rel,
                                )
                            )
                    elif _TAGGED_IMAGE_RE.match(image):
                        if pull_policy != "Never":
                            violations.append(
                                repo_invariants.Violation(
                                    f"{label} names a node-local tag but sets imagePullPolicy="
                                    f"{pull_policy!r}; this image is imported into containerd "
                                    "and exists in no registry, so it must be Never",
                                    rel,
                                )
                            )
                    else:
                        violations.append(
                            repo_invariants.Violation(
                                f"{label} is neither <app>:<tag> nor <registry>/<app>@sha256:"
                                "<64 hex>; the cluster cannot be shown to obtain it",
                                rel,
                            )
                        )

    return repo_invariants.RuleResult(
        "RULE 8",
        "platform app images name something the cluster can obtain",
        f"{checked} platform-app container{'s' if checked != 1 else ''} checked",
        tuple(violations),
    )


#: Single declared source for "has this app's registry push (G1a) actually
#: been switched on" — see _load_registry_push_status.
REGISTRY_PUSH_STATUS_FILE = pathlib.Path("scripts/registry-push-status.yaml")


def _load_registry_push_status(root: pathlib.Path) -> dict[str, bool]:
    """The one place that reads scripts/registry-push-status.yaml.

    vars.ZOT_PUSH_ENABLED gates the push step in each build-*.yml, but
    GitHub Actions repo variables live in repo settings, not this tree, and
    this checker is static: no network, no cluster, no credentials. This
    file is the checkable, repo-tracked proxy for that variable. A missing
    file or a missing app key means "not enabled" — fail closed, matching
    the actual state of a repository that has never set the variable — so a
    caller can never read an app as enabled by omission.
    """
    path = root / REGISTRY_PUSH_STATUS_FILE
    if not path.exists():
        return {}
    import yaml

    data = yaml.safe_load(path.read_text()) or {}
    return {str(app): bool(enabled) for app, enabled in data.items()}


def check_registry_push_capability_gates_digest_pins(root: pathlib.Path) -> repo_invariants.RuleResult:
    """A digest pin (the consumer) may not merge ahead of its app's registry
    push (the producer, G1a) being enabled.

    RULE 8 asks whether an image reference could ever resolve in principle —
    a shape question, answered from the manifest alone. It cannot tell a
    genuinely pushed digest from a well-formed one nobody ever pushed: both
    are 64 hex characters after ``@sha256:``. #476's placeholder was a
    degenerate case (repeated-hex) that a shape check happens to catch; a
    plausible but never-pushed digest is not, and RULE 8 has no data source
    that would let it ask. This rule asks the dependency question instead,
    against the single declared source in REGISTRY_PUSH_STATUS_FILE, and
    only for references RULE 8 would otherwise accept as resolvable — a
    placeholder is RULE 8's finding to report, not this rule's to double up
    on.
    """
    import yaml

    k8s_root = root / "infrastructure" / "k8s"
    status = _load_registry_push_status(root)
    violations: list[repo_invariants.Violation] = []
    checked = 0

    if k8s_root.is_dir():
        for path in sorted(k8s_root.rglob("*.y*ml")):
            rel = path.relative_to(root)
            if "secrets" in rel.parts:
                continue
            try:
                docs = list(yaml.safe_load_all(path.read_text()))
            except yaml.YAMLError:
                continue
            for doc in docs:
                if not isinstance(doc, dict):
                    continue
                for container in _iter_containers(doc):
                    image = str(container.get("image") or "")
                    app = _image_app_name(image)
                    if app not in PLATFORM_APP_IMAGE_NAMES:
                        continue
                    if _PLACEHOLDER_DIGEST_RE.match(image) or not _DIGEST_IMAGE_RE.match(image):
                        continue
                    checked += 1
                    if not status.get(app, False):
                        label = f"{rel}: container {container.get('name', '<unnamed>')} (image={image!r})"
                        violations.append(
                            repo_invariants.Violation(
                                f"{label} pins a registry digest for app {app!r}, but "
                                f"{REGISTRY_PUSH_STATUS_FILE} does not mark {app!r}'s "
                                "registry push as enabled. In a GitOps repository merge IS "
                                "deploy: the consumer of a capability (this digest pin) "
                                "cannot merge ahead of its producer (G1a's push to zot). "
                                f"Set {app!r}: true in {REGISTRY_PUSH_STATUS_FILE} only "
                                "after confirming that app's build workflow has actually "
                                "pushed an image.",
                                REGISTRY_PUSH_STATUS_FILE,
                            )
                        )

    return repo_invariants.RuleResult(
        "RULE 9",
        "digest-pinned images require an enabled registry push",
        f"{checked} digest-pinned platform-app container{'s' if checked != 1 else ''} checked",
        tuple(violations),
    )


def check_task_spec_requirement_refs_resolve(root: pathlib.Path) -> repo_invariants.RuleResult:
    """Every filed dev.task spec's requirement_refs must resolve, same as filing.

    ``file_task.py``'s ``_check_traceability`` refuses to file a spec whose
    ``requirement_refs`` point nowhere — but that refusal only ever runs once,
    at filing time. Nothing re-checks a spec already on disk, so a requirement
    later renamed or retired out from under a filed reference is never caught.
    This closes that gap using the identical resolver
    (``traceability.load_registries`` / ``Registry.resolves``), not a second
    implementation that could drift from what filing actually enforces.

    Scoped to the ``requirement_refs`` field only — the structured claim a spec
    makes, not prose. A citation inside a spec's free-text ``intent`` or
    ``_filing_note`` is exactly the kind of source/documentation citation
    ``requirement_citations.py`` reports without failing on; making that gate
    would fail specs for describing a defect in someone else's docstring.
    """
    registry = traceability.load_registries(root / REQUIREMENTS_DIR)
    if registry.directory_missing:
        # A missing registry can neither acquit every spec (0 violations,
        # falsely clean) nor accuse every spec (one violation per spec, falsely
        # blaming each citation) -- both misreport why nothing resolved. Fail
        # closed with a single violation naming the environment (PRIN-015),
        # the same shape check_requirement_citations_reported already uses for
        # requirement_citations.Report.scan_error.
        return repo_invariants.RuleResult(
            "task-spec-requirement-refs",
            "filed dev.task requirement_refs resolve",
            f"cannot evaluate: requirements registry not found ({root / REQUIREMENTS_DIR})",
            (
                repo_invariants.Violation(
                    "requirement registry directory not found "
                    f"({root / REQUIREMENTS_DIR}); a control that cannot ask "
                    "whether requirement_refs resolve must not report filed "
                    "specs as clean or as violations (PRIN-015). Restore "
                    "docs/requirements/ or run this check from a checkout "
                    "where it exists.",
                    REQUIREMENTS_DIR,
                ),
            ),
        )
    violations: list[repo_invariants.Violation] = []
    checked = 0

    tasks_dir = root / DEV_TASKS_DIR
    if not tasks_dir.is_dir():
        return repo_invariants.RuleResult(
            "task-spec-requirement-refs",
            "filed dev.task requirement_refs resolve",
            "tasks directory not present; nothing to check",
            (),
        )

    for path in sorted(tasks_dir.rglob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        refs = [str(r) for r in (data.get("requirement_refs") or [])]
        if not refs:
            continue
        checked += 1
        dangling = [r for r in refs if not registry.resolves(r)]
        if dangling:
            rel = path.relative_to(root)
            violations.append(
                repo_invariants.Violation(
                    f"{rel} carries requirement_refs that resolve to nothing: "
                    f"{', '.join(dangling)}",
                    rel,
                )
            )

    return repo_invariants.RuleResult(
        "task-spec-requirement-refs",
        "filed dev.task requirement_refs resolve",
        f"{checked} spec(s) with requirement_refs checked against "
        f"{', '.join(registry.sources) or '(no registries found)'}",
        tuple(violations),
    )


REQUIREMENT_CITATIONS_GRANDFATHER = pathlib.Path("scripts/requirement-citations-grandfathered.txt")


def _load_citation_grandfather(root: pathlib.Path) -> set[tuple[str, str]]:
    path = root / REQUIREMENT_CITATIONS_GRANDFATHER
    if not path.exists():
        return set()
    pairs: set[tuple[str, str]] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        parts = entry.split()
        if len(parts) != 2:
            raise ValueError(
                f"{REQUIREMENT_CITATIONS_GRANDFATHER}: expected '<path> <ref>', got {entry!r}"
            )
        pairs.add((parts[0], parts[1]))
    return pairs


def check_requirement_citations_reported(root: pathlib.Path) -> repo_invariants.RuleResult:
    """Every requirement-shaped citation in tracked source/docs resolves, or is
    grandfathered by name.

    The report-only era (see the git history of this docstring) existed
    because whether PC-ASR-006/007-shaped findings should be registered or
    corrected was a product-owner call this checker refused to make. That
    call was made 2026-09-07: #692 registered both from the behavior that
    cited them, leaving zero genuine live-code citations dangling. A
    dangling citation is now the defect it always looked like — except the
    deliberate ones (docstring examples, test fixtures, historical audit
    text), which are frozen by (path, ref) pair in
    ``scripts/requirement-citations-grandfathered.txt`` and admitted with a
    justification the file's own header demands.

    The ratchet cuts both ways: a grandfather entry that stops matching any
    dangling citation — the source line was fixed, the file moved, the ref
    was corrected — is a hole nothing else would ever report closed. This
    check fails on that entry by name, naming it for deletion, so staleness
    cannot hide in the list silently.

    A tree with no ``.git`` (a ``git archive`` export) cannot ask which files
    are tracked at all. Earlier, that made ``requirement_citations.audit``
    silently return an empty population, and this rule reported "0 dangling"
    — clean, because it never scanned anything. That is exactly what
    PRIN-015 forbids: an Unknown collapsing into a fixed value. This rule now
    fails as cannot-evaluate whenever the scan itself could not run — or
    whenever it ran and matched zero files, which is never evidence in a
    repository whose live scan covers over a thousand — rather than
    reporting a population of zero as a clean pass, and it does so before
    the grandfather-staleness pass: a run that scanned nothing can neither
    acquit nor accuse.
    """
    report = requirement_citations.audit(root)
    if report.scan_error is not None:
        return repo_invariants.RuleResult(
            "requirement-citations",
            "requirement citations resolve (gated; grandfather ratchet)",
            f"cannot evaluate: {report.scan_error}",
            (
                repo_invariants.Violation(
                    "requirement-citations could not determine which files are "
                    f"tracked ({report.scan_error}); a control that scanned zero "
                    "files must never report clean (PRIN-015). Run this check "
                    "from a git checkout with .git present, not an export.",
                    pathlib.Path("scripts/requirement_citations.py"),
                ),
            ),
        )
    if report.files_scanned == 0:
        return repo_invariants.RuleResult(
            "requirement-citations",
            "requirement citations resolve (gated; grandfather ratchet)",
            "cannot evaluate: the scan matched zero files",
            (
                repo_invariants.Violation(
                    "requirement-citations scanned zero files; in a repository "
                    "whose live scan covers over a thousand, zero is never "
                    "evidence — a run that scanned nothing can neither acquit "
                    "nor accuse (PRIN-015). Run this check from a git checkout "
                    "of the repository itself, not an export or an unrelated "
                    "work tree.",
                    pathlib.Path("scripts/requirement_citations.py"),
                ),
            ),
        )
    grandfathered = _load_citation_grandfather(root)
    matched: set[tuple[str, str]] = set()
    violations = []
    admitted = 0
    for citation in report.dangling:
        path = citation.path
        rel = (
            path.relative_to(root).as_posix()
            if path.is_absolute()
            else path.as_posix()
        )
        if (rel, citation.ref) in grandfathered:
            admitted += 1
            matched.add((rel, citation.ref))
            continue
        violations.append(
            repo_invariants.Violation(
                f"{rel}:{citation.line} cites {citation.ref}, which resolves in no "
                f"requirement registry. Register the requirement, correct the "
                f"citation, or — for a deliberate example/fixture — admit the "
                f"(path, ref) pair to {REQUIREMENT_CITATIONS_GRANDFATHER.as_posix()} "
                f"with a justification.",
                citation.path,
            )
        )

    # A grandfather entry that matches no live dangling citation is a standing
    # hole a future violation could hide in, with nothing left to report that
    # the hole stopped being needed. Fail on it by name rather than tolerate it.
    dead = sorted(grandfathered - matched)
    for dead_path, dead_ref in dead:
        violations.append(
            repo_invariants.Violation(
                f"{dead_path} {dead_ref} is grandfathered in "
                f"{REQUIREMENT_CITATIONS_GRANDFATHER.as_posix()} but matches no dangling "
                f"citation; delete this entry.",
                pathlib.Path(dead_path),
            )
        )

    summary = (
        f"{report.citations_checked} citation(s) across {report.files_scanned} tracked "
        f"file(s); {len(report.dangling)} dangling, {admitted} grandfathered"
    )
    if dead:
        summary += f", {len(dead)} grandfathered entry(ies) dead"
    return repo_invariants.RuleResult(
        "requirement-citations",
        "requirement citations resolve (gated; grandfather ratchet)",
        summary,
        tuple(violations),
    )


def check_principles_view(root: pathlib.Path) -> repo_invariants.RuleResult:
    """The principles file and the arch.principle beads agree — when a
    substrate credential is present to ask.

    ``principles_sync.py check-view`` has exited 1 correctly on drift since
    it shipped and was run by no CI job and no invariant — PRIN-016's own
    cited evidence. This rule is that mechanization. It bites wherever
    SUBSTRATE_URL and SUBSTRATE_API_KEY are in the environment (operator
    runs, the gate host, the worker checkout); plain CI carries no
    credential and gets an honest skip rather than an accusation — the same
    posture the loaders take (a credentialed scheduled check-view is
    chartered separately).
    """
    rule_id = "principles-view"
    name = "principles.md agrees with the arch.principle beads"
    if not (os.environ.get("SUBSTRATE_URL") and os.environ.get("SUBSTRATE_API_KEY")):
        return repo_invariants.RuleResult(
            rule_id, name, "not checked here: no substrate credential in the environment", ()
        )
    import principles_sync

    text = (root / "docs" / "architecture" / "principles.md").read_text(encoding="utf-8")
    try:
        store = principles_sync.Substrate()
        ok, diffs = principles_sync.check_view(store, text)
    except Exception as exc:
        return repo_invariants.RuleResult(
            rule_id,
            name,
            "view check could not run",
            (repo_invariants.Violation(
                f"principles_sync check-view failed to execute: {exc}",
                pathlib.Path("docs/architecture/principles.md"),
            ),),
        )
    if ok:
        return repo_invariants.RuleResult(rule_id, name, "file and beads agree", ())
    return repo_invariants.RuleResult(
        rule_id,
        name,
        f"{len(diffs)} drift(s) between principles.md and the beads",
        tuple(
            repo_invariants.Violation(diff, pathlib.Path("docs/architecture/principles.md"))
            for diff in diffs
        ),
    )


def _load_release_status():
    """``release-status.py`` cannot be ``import``-ed by name (the hyphen); the
    same loader `_load_ci_changes` uses for its own hyphenated sibling.

    Registered in ``sys.modules`` before execution, matching
    ``release-status.py``'s own ``_load_release_load_module`` note: its
    ``@dataclass``-decorated classes need to resolve their own module at
    class-creation time, which fails silently otherwise.
    """
    name = "release_status_invariant_check"
    path = pathlib.Path(__file__).resolve().with_name("release-status.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def check_unknown_value_never_collapses(root: pathlib.Path) -> repo_invariants.RuleResult:
    """PC-ASR-002/AC-1's enforcement: an Unknown value must read as Unknown
    everywhere -- at the source, after a JSON round trip, and at a real
    consumer -- and a real zero/empty must never be misread as Unknown either.

    Static analysis cannot establish this (the failure mode is a call site
    silently doing ``value or 0``), so this runs the actual primitive and a
    real consumer (``release-status.py``'s ``_criterion_status``) live. No
    substrate, no network: everything here is pure Python plus one hermetic
    module load.
    """
    violations: list[repo_invariants.Violation] = []
    checked = 0
    # Static, repo-relative -- this rule reasons about scripts/ itself
    # (this checker's own sibling modules), not about `root`, which a caller
    # may point at a synthetic tree for an unrelated rule's test.
    unknown_path = pathlib.Path("scripts/unknown.py")
    release_status_path = pathlib.Path("scripts/release-status.py")

    sample = unknown.Unknown(reason="check-repo-invariants self-check")
    transported = json.loads(json.dumps(unknown.to_jsonable(sample)))
    decoded = unknown.from_jsonable(transported)
    checked += 1
    if not unknown.is_unknown(decoded):
        violations.append(
            repo_invariants.Violation(
                "unknown.Unknown did not survive a JSON round trip: "
                f"transported={transported!r} decoded={decoded!r}",
                unknown_path,
            )
        )

    for flattened in (0, 0.0, "", None, [], {}):
        checked += 1
        if unknown.is_unknown(flattened):
            violations.append(
                repo_invariants.Violation(
                    f"unknown.is_unknown incorrectly reports {flattened!r} as unknown "
                    "-- a real zero/empty must never be misread as Unknown",
                    unknown_path,
                )
            )

    release_status = _load_release_status()
    malformed = release_status._criterion_status("not a real ref", [], "2026-01-01")
    checked += 1
    if not unknown.is_unknown(malformed.unmeasurable):
        violations.append(
            repo_invariants.Violation(
                "release-status.py's _criterion_status did not mark a malformed "
                "reference as Unknown -- it would render identically to a "
                "well-formed reference with zero recorded conformances",
                release_status_path,
            )
        )

    genuinely_empty = release_status._criterion_status("PC-FAKE-999/AC-1", [], "2026-01-01")
    checked += 1
    if unknown.is_unknown(genuinely_empty.unmeasurable):
        violations.append(
            repo_invariants.Violation(
                "release-status.py's _criterion_status marked a well-formed "
                "reference with zero recorded conformances as Unknown -- that is a "
                "real, verified absence (ABSENT's own kind of fact), not a "
                "computation failure",
                release_status_path,
            )
        )

    return repo_invariants.RuleResult(
        "unknown-value-integrity",
        "an Unknown value survives serialization and is never confused with zero",
        f"{checked} unknown-value propert{'y' if checked == 1 else 'ies'} checked",
        tuple(violations),
    )


#: R2603-5: the declared "platform core" surface — the files Amendment 30 /
#: R26.05 intends to publish — checked for zero household identifiers. This is
#: deliberately narrower than "every file under apps/factory-dispatcher/ and
#: scripts/": test fixtures, task specs, decision-record-style comments naming
#: who made a call, this household's own operational runbooks (LAN/DHCP,
#: offsite backup, the split-brain cluster guard, and similar scripts that
#: automate this household's physical infrastructure), and
#: scripts/release-manifest.py (whose generated instructions must stay correct
#: for a reviewer operating this household's real cluster today) are all
#: excluded on purpose — see .factory/design.md for the reasoning behind each.
#: A literal reappearing in one of the files below is the property this check
#: exists to catch; a literal appearing anywhere else in the repository is not
#: this check's concern.
HOUSEHOLD_IDENTIFIER_CORE_GLOBS = (
    "apps/factory-dispatcher/*.py",
    "apps/factory-dispatcher/activities/*.py",
    "apps/factory-dispatcher/workflows/*.py",
    "apps/factory-dispatcher/launchd/*.template",
    "scripts/ea-derive.py",
    "scripts/pr_base_guard.py",
    "scripts/regression-test.py",
)

#: Declared once, as short a list as covers every real occurrence found and
#: fixed for R2603-5. Each identifier is checked as a substring, so a variant
#: that embeds one of these (a kubeconfig filename like config-cluster-a,
#: a remote URL like git@github.com:grantbest/homelabv2.git) is caught by the
#: shorter root token without a redundant second entry.
HOUSEHOLD_IDENTIFIERS = (
    "host-c",
    "host-b",
    "cluster-a",
    "grantbest",
    "Truline",
)


def check_no_household_identifiers(root: pathlib.Path) -> repo_invariants.RuleResult:
    """R2603-5/AC-6: the platform core carries no household identifier.

    `worker_checkout.py` used to default the controlled checkout under one
    user's home; `dispatch.py` hardcoded this household's real GitHub repo as
    a startup default; `ea_observation.py` and `ea-derive.py` hardcoded this
    household's real cluster name. None of that was a defect for a private,
    single-family platform — until publishing three parts of it turns every
    compiled-in name into either a rewrite or a leak. This check makes "the
    core carries no household identifier" an enforced, standing property
    (checked every run of this script, one of this task's own declared
    verification commands) rather than a one-time cleanup that the next PR is
    free to quietly undo.

    WHAT THIS DOES NOT COVER, named rather than implied complete
    (release-gate finding on #642; the R2603-5 spec's own scope forbids these
    paths): the personal finance schema remains household-bound inside
    apps/substrate/ (its content models and ENCRYPTED_NAMESPACES are not yet
    a loadable extension), and hostnames survive in infrastructure/,
    apps/mcp-hub/ and apps/lifeops-console/. Those are the sibling work
    R26.05's publish arc charters; a reader must not take R2603-5 delivered
    as "the platform is de-householded" — only the dispatcher/scripts core is.
    """
    violations: list[repo_invariants.Violation] = []
    checked = 0

    paths: list[pathlib.Path] = []
    for pattern in HOUSEHOLD_IDENTIFIER_CORE_GLOBS:
        paths.extend(sorted(root.glob(pattern)))

    for path in paths:
        if not path.is_file():
            continue
        checked += 1
        rel = path.relative_to(root)
        try:
            lines = path.read_text().splitlines()
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(lines, start=1):
            # Case-insensitive: OPS-113 measured 'example.org' passing this check
            # while the list carried only 'Truline' -- `identifier in line` is a
            # literal substring test, so casing the list does not cover casing
            # in the file. Both sides are lower-cased rather than compiling the
            # list to `re.IGNORECASE` patterns, since every entry here is a
            # plain literal with no regex metacharacters to lose.
            line_lower = line.lower()
            for identifier in HOUSEHOLD_IDENTIFIERS:
                if identifier.lower() in line_lower:
                    violations.append(
                        repo_invariants.Violation(
                            f"{rel}:{lineno} names household identifier {identifier!r} in "
                            "the platform core. Move it behind configuration (R2603-5) — "
                            "an environment variable the core refuses to start without, "
                            "not a compiled-in default — or, if this is a decision record "
                            "or a household operational runbook rather than the core, take "
                            "it out of HOUSEHOLD_IDENTIFIER_CORE_GLOBS with a reason.",
                            rel,
                        )
                    )

    return repo_invariants.RuleResult(
        "no-household-identifiers",
        "the platform core carries no household identifier",
        f"{checked} core file{'s' if checked != 1 else ''} checked",
        tuple(violations),
    )


#: Directories pruned while walking apps/ and scripts/ for a created_by
#: fallback: the same "never actually collected/shipped" exclusions
#: _TEST_SCAN_EXCLUDED_DIRS already uses, plus "tests" and "migrations" --
#: a fixture asserting the very shape this rule forbids is not itself an
#: instance of the defect, and a migration's ``sa.Column("created_by", ...)``
#: declares a schema, not a write.
_CREATED_BY_SCAN_EXCLUDED_DIRS = _TEST_SCAN_EXCLUDED_DIRS | {"tests", "migrations"}


def _iter_created_by_candidate_files(root: pathlib.Path) -> Iterable[pathlib.Path]:
    """Production .py files inspected for a created_by fallback.

    apps/ and scripts/ only -- the same two trees the household's 32
    legitimate literal ``created_by=`` call keywords live in today. That
    count is an AST walk over non-test .py files in those two trees,
    counting ``created_by="<literal>"`` CALL KEYWORDS ONLY. Measured
    2026-09-18: 32 call keywords, 0 plain assignments, and 2 ANNOTATED
    class-attribute defaults (``created_by: str = "system"`` at
    apps/substrate/src/schemas.py:1768 and :1774) which are deliberately
    NOT counted -- they are schema field declarations, not a service
    naming itself at a call site. Counting them gives 34, so say which
    rule you used when re-measuring. A line-based grep reports a larger
    number again because it also matches prose inside docstrings.
    Test directories and pytest's own
    ``test_*.py``/``*_test.py`` naming convention are pruned for the same
    reason a fixture is excluded above.
    """
    for tree_root in (root / "apps", root / "scripts"):
        if not tree_root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(tree_root):
            dirnames[:] = [d for d in dirnames if d not in _CREATED_BY_SCAN_EXCLUDED_DIRS]
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                if filename.startswith("test_") or filename.endswith("_test.py"):
                    continue
                yield pathlib.Path(dirpath) / filename


def _string_literal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        # An f-string with no interpolated parts (e.g. f"unknown") is a
        # literal by any reading. One that interpolates something contains
        # a FormattedValue part and returns None -- that is a computed
        # value, not a fabricated one.
        parts: list[str] = []
        for part in node.values:
            if not isinstance(part, ast.Constant) or not isinstance(part.value, str):
                return None
            parts.append(part.value)
        return "".join(parts)
    return None


def _is_lookup_expr(node: ast.AST) -> bool:
    return isinstance(node, (ast.Attribute, ast.Subscript, ast.Call))


def _lookup_root_name(node: ast.AST) -> str | None:
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Call)):
        node = node.value if isinstance(node, (ast.Attribute, ast.Subscript)) else node.func
    return node.id if isinstance(node, ast.Name) else None


def _none_checked_name(test: ast.AST) -> tuple[str, bool] | None:
    """``(name, is_not_none)`` if ``test`` is ``<name> is [not] None``, the
    reverse operand order -- ``None is [not] <name>`` -- or a bare
    truthiness test on ``name`` (``<name>`` / ``not <name>``).

    A truthiness test is one token away from the ``is not None`` form and is
    the likeliest way that shape gets rewritten, so it is treated exactly
    the same: ``identity if identity else "x"`` and ``identity if identity
    is not None else "x"`` are the same defect for this rule's purposes.
    """
    if isinstance(test, ast.Name):
        return test.id, True
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) and isinstance(test.operand, ast.Name):
        return test.operand.id, False
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or len(test.comparators) != 1:
        return None
    op = test.ops[0]
    if not isinstance(op, (ast.Is, ast.IsNot)):
        return None
    is_not_none = isinstance(op, ast.IsNot)
    left, right = test.left, test.comparators[0]
    if isinstance(right, ast.Constant) and right.value is None and isinstance(left, ast.Name):
        return left.id, is_not_none
    if isinstance(left, ast.Constant) and left.value is None and isinstance(right, ast.Name):
        return right.id, is_not_none
    return None


def _identity_fallback_reason(value: ast.AST) -> str | None:
    """If ``value`` substitutes a literal for a failed identity lookup, why.

    Three shapes, all naming the same defect (a real write recorded under a
    fabricated actor instead of failing loudly): a None-guarded conditional,
    ``or`` with a literal default, and ``getattr``'s positional default.

    The conditional and ``or`` shapes both require the non-literal branch to
    be a *lookup* -- an attribute access, subscript, or call -- not a bare
    name. That is what makes it "an identity lookup yields nothing" rather
    than "an already-optional plain value defaults to a placeholder": e.g.
    ``x_created_by or "unknown"`` where ``x_created_by`` is itself an
    optional HTTP header is a different, legitimate shape this rule does not
    concern itself with (measured on main: apps/substrate/src/routes.py's
    ``create_bead_link``). ``identity.client if identity is not None else
    "unknown"`` dereferences the checked name itself, which is the shape
    this rule exists for.
    """
    if isinstance(value, ast.IfExp):
        checked = _none_checked_name(value.test)
        if checked is None:
            return None
        name, is_not_none = checked
        none_branch = value.orelse if is_not_none else value.body
        real_branch = value.body if is_not_none else value.orelse
        literal = _string_literal(none_branch)
        if literal is None or not _is_lookup_expr(real_branch):
            return None
        if _lookup_root_name(real_branch) != name:
            return None
        return f"substitutes literal {literal!r} for {name}'s lookup when {name} is None"

    if isinstance(value, ast.BoolOp) and isinstance(value.op, ast.Or) and len(value.values) == 2:
        real_branch, fallback = value.values
        literal = _string_literal(fallback)
        if literal is None or not _is_lookup_expr(real_branch):
            return None
        return f"substitutes literal {literal!r} when the identity lookup is falsy"

    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "getattr":
        if len(value.args) >= 3:
            literal = _string_literal(value.args[2])
            if literal is not None:
                return f"substitutes literal {literal!r} as getattr's default when the identity attribute is missing"

    return None


def _is_created_by_target(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "created_by"
    if isinstance(node, ast.Attribute):
        return node.attr == "created_by"
    if isinstance(node, ast.Subscript):
        return _string_literal(node.slice) == "created_by"
    return False


def _created_by_fallback_hits(tree: ast.Module) -> Iterable[tuple[int, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            if not any(_is_created_by_target(target) for target in node.targets):
                continue
            reason = _identity_fallback_reason(node.value)
            if reason is not None:
                yield node.lineno, reason
        elif isinstance(node, ast.AnnAssign):
            if node.value is None or not _is_created_by_target(node.target):
                continue
            reason = _identity_fallback_reason(node.value)
            if reason is not None:
                yield node.lineno, reason
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg != "created_by":
                    continue
                reason = _identity_fallback_reason(keyword.value)
                if reason is not None:
                    yield keyword.value.lineno, reason
        elif isinstance(node, ast.Dict):
            for key, dict_value in zip(node.keys, node.values):
                if key is None or _string_literal(key) != "created_by":
                    continue
                reason = _identity_fallback_reason(dict_value)
                if reason is not None:
                    yield dict_value.lineno, reason


#: Pre-existing created_by fallback instances this rule would otherwise
#: flag, not corrected here because apps/** is out of this structural task's
#: declared scope (scripts/check-repo-invariants.py, scripts/repo_invariants.py,
#: scripts/tests/ only). One "repo/relative/path.py:lineno" per line.
#: APPEND-ONLY except to remove an entry once the named line has actually
#: been fixed -- mirrors scripts/change-kind-grandfathered.txt's ratchet.
#: Declared as a constant here, rather than a sibling scripts/*.txt file
#: (private-identifier-grandfathered.txt's style), only because a new
#: top-level scripts/ file is itself outside this task's declared scope.
#:
#: Empty, and that is the intended end state: there is no live created_by
#: fallback in production. #938 removed the last one
#: (apps/mcp-hub/src/routers/v1/factory.py:68, replaced by an explicit
#: raise), so this rule now guards a clean tree rather than ratcheting down
#: from a known offender.
#:
#: Exempts by exact line, not by path, should an entry ever be needed again:
#: the recurrence this rule exists to catch is a second, undeclared fallback
#: landing in the *same* file next to an already-grandfathered one. Measured
#: 2026-09-18: #935 alone, against main, takes
#: apps/mcp-hub/src/routers/v1/factory.py from one instance to two (:68 and
#: :162); composing #938 with #935 leaves one (:173), because #938 removes
#: :68. A path-wide exemption would blind the rule to exactly that shape.
#: If a future entry's line number drifts, the check starts failing at the
#: new line -- update the entry to match, do not widen it to the whole file.
#:
#: A stale entry -- one naming a line that no longer holds a fallback -- is
#: itself a violation, so this tuple cannot silently outlive the thing it
#: exempts.
CREATED_BY_FALLBACK_GRANDFATHERED: tuple[str, ...] = ()


def check_created_by_never_fabricates_identity(root: pathlib.Path) -> repo_invariants.RuleResult:
    """A provenance created_by must never fall back to a placeholder actor.

    Deliberately separate from check_unknown_value_never_collapses just
    above, rather than an extension of it: that rule is about
    ``unknown.Unknown``, a SENTINEL TYPE, surviving a JSON round trip
    without being confused with a real zero/empty value. This rule is about
    a plain *string* substituted for a real actor when an identity lookup
    fails -- a different question the two share only the English word
    "unknown". Folding this into that rule would conflate them.

    Deliberately narrow in the other direction too: a PLAIN LITERAL
    ``created_by`` (``created_by="notify/alert_state"``, 32 of them in
    production today -- AST call-keyword count over non-test apps/ and
    scripts/, excluding annotated class-attribute defaults; see
    _iter_created_by_candidate_files for the counting rule) is a service
    correctly naming itself, and is not
    flagged -- only a conditional/``or``/``getattr`` substituting a literal
    for a failed identity *lookup* counts. See _identity_fallback_reason.
    """
    grandfathered = set(CREATED_BY_FALLBACK_GRANDFATHERED)
    matched: set[str] = set()
    scanned_paths: set[str] = set()
    violations: list[repo_invariants.Violation] = []
    checked = 0
    skipped_grandfathered = 0

    for path in sorted(_iter_created_by_candidate_files(root)):
        checked += 1
        rel = repo_invariants._relative(path, root)
        scanned_paths.add(rel.as_posix())
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(rel))
        except SyntaxError as exc:
            violations.append(repo_invariants.Violation(f"{rel} could not be parsed: {exc}", rel))
            continue
        for lineno, reason in _created_by_fallback_hits(tree):
            entry = f"{rel.as_posix()}:{lineno}"
            if entry in grandfathered:
                matched.add(entry)
                skipped_grandfathered += 1
                continue
            violations.append(
                repo_invariants.Violation(
                    f"{rel}:{lineno} {reason} -- a real write would be recorded under a "
                    "fabricated actor instead of failing. Raise instead of falling back, "
                    "or if this is genuinely pre-existing debt out of scope to fix here, "
                    f"add {entry!r} to CREATED_BY_FALLBACK_GRANDFATHERED with a reason.",
                    rel,
                )
            )

    # A guard that cannot observe must say so rather than accuse (house
    # precedent: check_pr_merge_commit_change_kind's baseline_reachable
    # guard): a synthetic fixture tree built for an unrelated rule's test,
    # or this rule's own tests, never contains
    # apps/mcp-hub/src/routers/v1/factory.py at all, and reporting that
    # entry as stale there would manufacture a finding about a file that was
    # never scanned. Only a grandfather entry whose named FILE was actually
    # walked, and still didn't match, is genuinely stale.
    for entry in sorted(grandfathered - matched):
        rel_path = entry.split(":", 1)[0]
        if rel_path not in scanned_paths:
            continue
        violations.append(
            repo_invariants.Violation(
                f"{entry} is listed in CREATED_BY_FALLBACK_GRANDFATHERED but matches no "
                "created_by fallback any more -- fixed already, or the line drifted. "
                "Remove or update this entry.",
                pathlib.Path(rel_path),
            )
        )

    summary = f"{checked} production file(s) checked for a created_by fallback"
    if skipped_grandfathered:
        summary += f", {skipped_grandfathered} grandfathered"
    return repo_invariants.RuleResult(
        "created-by-never-fabricates-identity",
        "created_by never falls back to a fabricated actor",
        summary,
        tuple(violations),
    )


def run_checks(root: pathlib.Path = REPO) -> list[repo_invariants.RuleResult]:
    root = root.resolve()
    return [
        *repo_invariants.run_checks(root),
        check_lint_workflow_python_test_dependencies(root),
        check_test_map_covers_apps(root),
        check_job_classes_complete(root),
        check_no_colliding_test_module_basenames(root),
        check_platform_app_images_are_pinned(root),
        check_registry_push_capability_gates_digest_pins(root),
        check_task_spec_requirement_refs_resolve(root),
        check_requirement_citations_reported(root),
        check_principles_view(root),
        check_unknown_value_never_collapses(root),
        check_no_household_identifiers(root),
        check_created_by_never_fabricates_identity(root),
        private_identifiers.check_publishable_paths_carry_no_private_identifiers(root),
        private_identifiers.check_publishable_scope_outcomes_are_release_prefixed_and_resolve(root),
        readme_conformance.check_readme_model_conformance(root),
    ]


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
    print(repo_invariants.format_results(results))
    return 1 if any(result.violations for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
