"""Reproduces the ``substrate_client`` bare-name collision (dev.finding
d9ae4d93) and proves the five scripts/ consumers no longer lose the name.

Two different things answer to the importable name ``substrate_client``:
``scripts/substrate_client.py`` (a module defining, among others, the
``SubstrateClient`` Protocol) and the package at
``apps/substrate/client/src/substrate_client`` (whose ``__all__`` does not
include ``SubstrateClient`` at all). Whichever of the two is imported first
under the bare name wins ``sys.modules["substrate_client"]`` for the rest of
the process -- PR #878 hit this by adding a module-scope import of the
package, which made ``scripts/requirements-load.py``'s own bare import
resolve to the package and fail with
``ImportError: cannot import name 'SubstrateClient'``.

This test forces that same collision -- the package wins the bare name
first -- then loads each of ``ea-load.py``, ``principles_sync.py``,
``release-load.py``, ``release-status.py`` and ``requirements-load.py`` and
asserts each still resolves its symbols from ``scripts/substrate_client.py``,
by identity, not merely by name.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO / "scripts"
PACKAGE_INIT = REPO / "apps" / "substrate" / "client" / "src" / "substrate_client" / "__init__.py"

#: The alias every one of the five scripts caches scripts/substrate_client.py
#: under once loaded by file path -- see e.g. ea-load.py's own
#: _load_substrate_client_module. Loading the real module under this exact
#: key here, before any script runs, makes every script's own cache lookup
#: hit this same object: a script that resolved the wrong thing (the
#: package, or a stray re-exec) would fail the identity assertions below,
#: not merely produce a same-named class.
_SUBSTRATE_CLIENT_ALIAS = "_scripts_substrate_client_impl"

#: (module alias for this test, script filename)
_SCRIPTS = [
    ("ea_load_collision", "ea-load.py"),
    ("principles_sync_collision", "principles_sync.py"),
    ("release_load_collision", "release-load.py"),
    ("release_status_collision", "release-status.py"),
    ("requirements_load_collision", "requirements-load.py"),
]


def _load_by_path(alias, path, *, package_dir=None):
    kwargs = {}
    if package_dir is not None:
        kwargs["submodule_search_locations"] = [str(package_dir)]
    spec = importlib.util.spec_from_file_location(alias, path, **kwargs)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def collision():
    """Force the apps/substrate/client package to occupy the bare name
    ``substrate_client`` before any of the five scripts load -- PR #878's
    shape -- then restore every sys.modules entry this test touches so it
    cannot leak state into a test collected after it.
    """
    saved = {
        key: sys.modules.get(key)
        for key in ["substrate_client", _SUBSTRATE_CLIENT_ALIAS]
        + [alias for alias, _ in _SCRIPTS]
    }
    _load_by_path("substrate_client", PACKAGE_INIT, package_dir=PACKAGE_INIT.parent)
    try:
        yield
    finally:
        for key, module in saved.items():
            if module is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = module


def test_bare_name_collision_is_reproduced(collision):
    """Sanity check on the fixture: the package, not the module, now answers
    to the bare name, and it does not export SubstrateClient at all -- the
    exact shape of PR #878's failure."""
    package = sys.modules["substrate_client"]
    assert not hasattr(package, "SubstrateClient")
    assert hasattr(package, "Substrate")


def test_five_scripts_resolve_substrate_client_despite_the_collision(collision):
    reference = _load_by_path(_SUBSTRATE_CLIENT_ALIAS, SCRIPTS_DIR / "substrate_client.py")
    assert reference.__file__ == str(SCRIPTS_DIR / "substrate_client.py")

    loaded = {alias: _load_by_path(alias, SCRIPTS_DIR / filename) for alias, filename in _SCRIPTS}
    ea_load = loaded["ea_load_collision"]
    principles_sync = loaded["principles_sync_collision"]
    release_load = loaded["release_load_collision"]
    release_status = loaded["release_status_collision"]
    requirements_load = loaded["requirements_load_collision"]

    assert ea_load._Substrate is reference.Substrate
    assert ea_load.SubstrateError is reference.SubstrateError

    assert principles_sync._Substrate is reference.Substrate

    assert release_load._Substrate is reference.Substrate
    assert release_load.SubstrateClient is reference.SubstrateClient
    assert release_load.SubstrateError is reference.SubstrateError

    assert requirements_load._Substrate is reference.Substrate
    assert requirements_load.SubstrateClient is reference.SubstrateClient
    assert requirements_load.SubstrateError is reference.SubstrateError

    assert release_status.substrate_client is reference
