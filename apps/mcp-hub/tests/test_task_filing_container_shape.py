"""Reproduces the Dockerfile's own layout for tools.task_filing's import of
the dispatcher tree -- the same technique test_container_startup_import.py
already uses for tools.factory_status, extended to the four trees AC-1
requires the image to ship (see .factory/design.md, "Discovery 2"):

    /app/src/                     mcp-hub's own app (unchanged)
    /app/apps/factory-dispatcher/ file_task.py, dispatch.py, etc.
    /app/scripts/                 traceability.py, substrate_client.py
    /app/apps/substrate/src/      schemas.py (BeadProvenance, validators)
    /app/apps/substrate/client/   the M6 substrate_client package

Each of these paths is not arbitrary: file_task.py, failure_diagnosis.py (a
transitive import of dispatch.py), substrate_client_loader.py and
scripts/substrate_client.py each derive a sibling path from their OWN
__file__ location, and those derivations only resolve correctly when this
relative layout matches a real checkout exactly -- the same class of defect
#614 shipped for tools/factory_status.py, reproduced here at the image's own
path depth rather than reasoned about.

A subprocess is used, not an in-process import, for the same reason
test_container_startup_import.py gives: this can't pass by accident from
another test having already imported (and cached in sys.modules) the real
dispatch/file_task modules from the checkout path earlier in the same test
session.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MCP_HUB_SRC = REPO_ROOT / "apps" / "mcp-hub" / "src"
DISPATCHER_SRC = REPO_ROOT / "apps" / "factory-dispatcher"
SCRIPTS_SRC = REPO_ROOT / "scripts"
SUBSTRATE_SRC = REPO_ROOT / "apps" / "substrate" / "src"
SUBSTRATE_CLIENT_SRC = REPO_ROOT / "apps" / "substrate" / "client"

_IGNORE = shutil.ignore_patterns("tests", "__pycache__", "*.pyc", "tasks")


REQUIREMENTS_SRC = REPO_ROOT / "docs" / "requirements"


def _build_container_root(dest: Path, *, include_requirements: bool = True) -> Path:
    """Reproduce the image layout. ``include_requirements`` exists so the
    absence of the registry can be driven as a known-bad control -- it is the
    shape the image actually had until the #909 gate found it (F1).
    """
    container_root = dest / "app"
    shutil.copytree(MCP_HUB_SRC, container_root / "src")
    shutil.copytree(
        DISPATCHER_SRC, container_root / "apps" / "factory-dispatcher", ignore=_IGNORE
    )
    shutil.copytree(SCRIPTS_SRC, container_root / "scripts", ignore=_IGNORE)
    shutil.copytree(
        SUBSTRATE_SRC, container_root / "apps" / "substrate" / "src", ignore=_IGNORE
    )
    shutil.copytree(
        SUBSTRATE_CLIENT_SRC, container_root / "apps" / "substrate" / "client", ignore=_IGNORE
    )
    if include_requirements:
        shutil.copytree(REQUIREMENTS_SRC, container_root / "docs" / "requirements")
    return container_root


def test_task_filing_loads_and_files_from_a_container_shaped_path(tmp_path):
    container_root = _build_container_root(tmp_path)

    env = dict(os.environ)
    env.pop("FACTORY_STATUS_REPO_ROOT", None)
    env["FACTORY_DISPATCHER_ROOT"] = str(container_root)
    env["PYTHONPATH"] = str(container_root / "src")

    probe = (
        "import sys; "
        "from tools import task_filing; "
        "file_task = task_filing._load_file_task(); "
        "print('IMPORT_OK', file_task.__file__); "
        "print('DISPATCHER_DIR_OK', str(task_filing._dispatcher_dir()))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=container_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, (
        f"task_filing failed to load from a container-shaped path "
        f"(cwd={container_root}, PYTHONPATH={env['PYTHONPATH']}):\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "IMPORT_OK" in result.stdout
    # And it resolved to the COPY inside the container root, not by falling
    # back to this checkout's real apps/factory-dispatcher some other way.
    assert str(container_root) in result.stdout


# ---------------------------------------------------------------------------
# ADDED 2026-09-17 at the #909 gate (F1). The test above is named
# "..._loads_and_files_from_a_container_shaped_path" and it never filed: its
# probe called _load_file_task() and _dispatcher_dir() only. That is why the
# image shipping no docs/requirements/ reached a gate instead of a test.
#
# file_task.py:61 derives REQUIREMENTS_DIR as parents[2]/docs/requirements,
# which is /app/docs/requirements in the image, and build_content() runs
# _check_traceability() BEFORE any substrate call -- so a spec that CITES a
# real requirement is the first thing an absent registry refuses, while a spec
# that WAIVES traceability sails through. No store is needed to drive it, and
# using build_content() rather than file_spec() keeps the test free of a fake
# store while exercising the exact function that refuses.
# ---------------------------------------------------------------------------

_SPEC_CITING_A_REAL_REQUIREMENT = """{
  "lane": "code-health",
  "title": "container-shape probe",
  "intent": "probe",
  "acceptance": ["AC-1: probe"],
  "scope": {"paths": ["apps/mcp-hub/"]},
  "risk_class": "behavioral",
  "requirement_refs": ["PC-FAC-001/AC-5"],
  "principal": "test fixture; exercises traceability from a container-shaped path, not C-4",
  "reaches": ["none"]
}"""

_BUILD_PROBE = (
    "import json; "
    "from tools import task_filing; "
    "file_task = task_filing._load_file_task(); "
    "spec = json.loads(%r); "
    "file_task.build_content(spec); "
    "print('TRACEABILITY_OK', file_task.REQUIREMENTS_DIR)"
) % _SPEC_CITING_A_REAL_REQUIREMENT


def _run_probe(container_root: Path):
    env = dict(os.environ)
    env.pop("FACTORY_STATUS_REPO_ROOT", None)
    env["FACTORY_DISPATCHER_ROOT"] = str(container_root)
    env["PYTHONPATH"] = str(container_root / "src")
    return subprocess.run(
        [sys.executable, "-c", _BUILD_PROBE],
        cwd=container_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_a_spec_citing_a_real_requirement_files_from_a_container_shaped_path(tmp_path):
    result = _run_probe(_build_container_root(tmp_path))

    assert result.returncode == 0, (
        "a spec citing a real requirement was refused from a container-shaped path; "
        "the image is missing docs/requirements/ (#909 gate, F1):\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "TRACEABILITY_OK" in result.stdout
    # and it resolved the registry INSIDE the container root, not by reaching
    # back into this checkout
    assert str(tmp_path / "app") in result.stdout, result.stdout


def test_the_probe_detects_an_image_with_no_requirements_registry(tmp_path):
    """The known-bad control. Without this, the test above could pass because it
    silently reached the real checkout's registry rather than the container's,
    and a passing probe and an inert one would look identical."""
    result = _run_probe(_build_container_root(tmp_path, include_requirements=False))

    assert result.returncode != 0, (
        "the probe passed with NO requirements registry in the container root, so "
        "it is not actually measuring the container's own tree:\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    # The refusal text moved between this PR's base and the merged tree, and
    # it moved in the right direction. At base 160e812f an absent registry
    # DIRECTORY fell through to the dangling-reference branch and was reported
    # as "requirement reference(s) resolve to nothing ... (no registries
    # found)" -- blaming the citation for the environment's fault. #838
    # (merged 2026-09-17 as c41c9000) added a `registry.directory_missing`
    # branch AHEAD of it that says so plainly instead. That is exactly the
    # condition this control constructs, so this control now asserts the
    # precise message rather than the fall-through one.
    assert "no requirements registry was found" in result.stderr, result.stderr
    assert "the directory does not exist here" in result.stderr, result.stderr
    assert "environment fault, not a bad citation" in result.stderr, result.stderr


# ---------------------------------------------------------------------------
# The probe above builds its OWN container root, so it proves the path
# arithmetic and nothing about the real image. These two close that loop: the
# tree only reaches /app if the Dockerfile copies it AND the root .dockerignore
# whitelist admits it. Either one missing reproduces F1, and neither is visible
# to a test that constructs its own tree.
# ---------------------------------------------------------------------------


def test_the_dockerfile_copies_the_requirements_registry():
    dockerfile = (REPO_ROOT / "apps" / "mcp-hub" / "Dockerfile").read_text()
    assert "COPY docs/requirements/" in dockerfile, (
        "the image must carry docs/requirements/: file_task.py resolves "
        "REQUIREMENTS_DIR to /app/docs/requirements and _check_traceability runs "
        "before any substrate call, so without it the HTTP intake refuses every "
        "spec citing a real requirement (#909 gate, F1)"
    )


def test_the_build_context_admits_the_requirements_registry():
    dockerignore = (REPO_ROOT / ".dockerignore").read_text()
    assert "!docs/requirements/" in dockerignore, (
        "the root .dockerignore is a whitelist, so a COPY of an unadmitted tree "
        "silently copies nothing; docs/requirements/ must be re-admitted (#909 gate, F1)"
    )
