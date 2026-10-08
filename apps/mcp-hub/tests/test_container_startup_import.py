"""Reproduces the Dockerfile's own layout for the app's startup import.

apps/mcp-hub/Dockerfile: ``WORKDIR /app``, ``COPY src/ ./src/``, then
``uvicorn openapi_app:app --app-dir src``. The image ships *only* that
``src/`` tree -- no sibling ``apps/factory-dispatcher``, no sibling
``scripts/`` -- and #614 shipped a ``tools/factory_status.py`` that derived
its own location with fixed-depth ``__file__.resolve().parents[N]``
arithmetic, which happened to be a repo root in a checkout and out of range
in the image, raising ``IndexError`` at import time and taking the whole
app down before uvicorn could bind (#756's build-host smoke test is what
caught this in CI; this test asserts the same property directly, in-process,
without a docker build).

Copies the real ``src/`` tree into ``<tmp>/app/src`` -- an actual directory
layout shaped like the image, not a symlink (``Path.resolve()`` would just
follow a symlink straight back to the checkout and defeat the point) -- and
imports the startup module the same way the Dockerfile's CMD does:
``PYTHONPATH`` set to ``<tmp>/app/src``, cwd at ``<tmp>/app``, and
``FACTORY_STATUS_REPO_ROOT`` unset, because the image never sets it either.
A subprocess is used rather than an in-process import so this can't pass by
accident from another test having already imported (and cached in
``sys.modules``) the real ``openapi_app``/``tools.factory_status`` from the
checkout path earlier in the same test session.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

MCP_HUB_SRC = Path(__file__).resolve().parents[1] / "src"


def test_factory_status_does_not_derive_paths_from_parent_depth():
    """Pins the fix structurally, independent of host filesystem depth.

    A fixed parent index re-added at a shallower depth would not raise in
    the copied-tree test below -- it would silently resolve to whatever real
    directory happens to be that many levels up, which is the "loud
    IndexError trades for a silent wrong answer" trap called out in this
    fix. So the property is also pinned structurally here: the module must
    not derive REPO_ROOT/DISPATCHER_DIR/SCRIPTS_DIR from ``__file__``'s
    parent count at all.

    CORRECTED AT THE GATE 2026-09-12: an earlier version of this docstring
    said reproducing #614's exact ``IndexError`` "needs the copy to sit
    directly under the filesystem root -- not available to a sandboxed test
    run (or most CI runners) without root", and offered this grep as the
    only available substitute. That is false, and the release gate
    disproved it in five lines. ``Path.resolve()`` does not require a path
    to exist, so ``/app/src/tools/factory_status.py`` yields exactly four
    parents on any host whether or not ``/app`` is present -- and
    ``test_module_loads_at_the_images_own_path_depth`` below now reproduces
    the real defect exactly, with no root and no docker. This test remains
    useful (it pins the spelling cheaply) but it is NOT the guarantee: on
    its own it passes against a module that re-derives depth by a different
    spelling.
    """
    source = (MCP_HUB_SRC / "tools" / "factory_status.py").read_text()
    assert ".parents[" not in source
    assert "FACTORY_STATUS_REPO_ROOT" in source


def test_startup_module_imports_from_a_container_shaped_path(tmp_path):
    container_root = tmp_path / "app"
    shutil.copytree(MCP_HUB_SRC, container_root / "src")

    env = dict(os.environ)
    env.pop("FACTORY_STATUS_REPO_ROOT", None)
    env["PYTHONPATH"] = str(container_root / "src")

    result = subprocess.run(
        [sys.executable, "-c", "import openapi_app; print('IMPORT_OK')"],
        cwd=container_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, (
        f"startup import failed from a container-shaped path "
        f"(cwd={container_root}, PYTHONPATH={env['PYTHONPATH']}):\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "IMPORT_OK" in result.stdout


def test_module_loads_at_the_images_own_path_depth(monkeypatch):
    """The real defect, reproduced exactly: four parents, no root, no docker.

    ``Path.resolve()`` does not stat, so compiling the module's source with
    ``__file__`` set to the image's literal path gives it precisely the
    parent count it has in the container (indices 0-3) regardless of where
    this test runs. Against #614's ``parents[4]`` this raises
    ``IndexError: 4`` -- the exact failure that took the app down before
    uvicorn could bind and that #756's smoke test caught at the build host.

    This is the test that carries AC-3's guarantee. The copied-tree test
    above cannot: verified at the gate by reverting factory_status.py alone
    to its pre-fix state, where the copied-tree test still PASSED and only
    the source grep failed.
    """
    monkeypatch.delenv("FACTORY_STATUS_REPO_ROOT", raising=False)
    image_path = "/app/src/tools/factory_status.py"

    assert len(Path(image_path).parents) == 4, (
        "the image's own depth is the premise of this test; if this ever "
        "changes, the Dockerfile's WORKDIR/COPY layout changed with it"
    )

    source = (MCP_HUB_SRC / "tools" / "factory_status.py").read_text()
    namespace = {"__file__": image_path, "__name__": "probe_factory_status"}

    # Must not raise. #614's module raises IndexError: 4 right here.
    exec(compile(source, image_path, "exec"), namespace)

    # And the absent-configuration state is the declared one, not a path
    # that silently resolves somewhere real.
    assert namespace["REPO_ROOT"] is None
    assert namespace["DISPATCHER_DIR"] is None
    assert namespace["SCRIPTS_DIR"] is None
