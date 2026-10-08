"""``substrate_client``'s public surface is the BeadStore protocol plus one
generic method (``search``) this package adds beyond that protocol, plus
SubstrateReader -- pinned mechanically (PRIN-005) rather than left to a
docstring, the same pattern
``apps/factory-dispatcher/tests/test_store_call_surface.py`` and
``scripts/tests/test_substrate_client.py`` (``test_public_surface_is_pinned``)
already use for the two clients this package unions.

The BeadStore protocol itself (``apps/factory-dispatcher/beadstore.py``) is
frozen at 14 -- ``create_task`` stays narrow there, deliberately, per
``client.py``'s module docstring; ``create_bead`` and ``patch_context``
joined the protocol itself at OPS-190, no longer counted as this package's
own extra surface. The remaining extra (``search``) lives only on
``Substrate``, which is why the two facts below are pinned separately: a
widening of the protocol itself should show up as a change to the 14, and a
widening of this client's own surface should show up as a change to the 15.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_DISPATCHER_DIR = _REPO_ROOT / "apps" / "factory-dispatcher"
_NEW_CLIENT_PKG_INIT = (
    _REPO_ROOT / "apps" / "substrate" / "client" / "src" / "substrate_client" / "__init__.py"
)

if str(_DISPATCHER_DIR) not in sys.path:
    sys.path.insert(0, str(_DISPATCHER_DIR))

from beadstore import BeadStore  # noqa: E402


def _load_new_substrate_client():
    """Load this package by explicit file path, under an alias distinct from
    the bare name ``substrate_client``.

    scripts/substrate_client.py is ALSO importable as ``substrate_client``
    (deliberately -- see this package's own docstring), and a dispatcher
    module collected earlier in the same pytest session
    (``file_task.py``/``test_ea_observation.py`` both prepend ``scripts/`` to
    ``sys.path``) can leave that OLD module cached under the bare name in
    ``sys.modules`` before this file ever runs. An ordinary
    ``import substrate_client`` would then silently pin this suite to the
    old client instead of the one under test.
    """
    alias = "new_substrate_client_for_public_surface_test"
    spec = importlib.util.spec_from_file_location(
        alias,
        _NEW_CLIENT_PKG_INIT,
        submodule_search_locations=[str(_NEW_CLIENT_PKG_INIT.parent)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


substrate_client = _load_new_substrate_client()

_BEADSTORE_PROTOCOL_METHODS = {
    name
    for name in dir(BeadStore)
    if not name.startswith("_") and callable(getattr(BeadStore, name, None))
}


def test_beadstore_protocol_is_exactly_fourteen_methods():
    """Pins the number the bead's intent names, so a widening or narrowing of
    the protocol itself is visible here rather than only where it bites."""
    assert len(_BEADSTORE_PROTOCOL_METHODS) == 14


_GENERIC_METHODS_BEYOND_THE_PROTOCOL = {"search"}


def test_substrate_public_surface_is_the_beadstore_protocol_plus_generic_search():
    public = {
        name
        for name, _ in inspect.getmembers(substrate_client.Substrate, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    assert public == _BEADSTORE_PROTOCOL_METHODS | _GENERIC_METHODS_BEYOND_THE_PROTOCOL


def test_substrate_public_surface_count_is_pinned_at_fifteen():
    """15 = the 14 BeadStore protocol methods plus search. A future
    addition is a visible change to this literal, not silent drift -- the
    same discipline test_beadstore_protocol_is_exactly_fourteen_methods
    already applies to the narrower protocol."""
    public = {
        name
        for name, _ in inspect.getmembers(substrate_client.Substrate, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    assert len(public) == 15


def test_substrate_reader_public_surface_is_get_only():
    public = {
        name
        for name, _ in inspect.getmembers(
            substrate_client.SubstrateReader, predicate=inspect.isfunction
        )
        if not name.startswith("_")
    }
    assert public == {"get"}


def test_substrate_structurally_satisfies_the_beadstore_protocol():
    client = substrate_client.Substrate(base_url="http://substrate.test", api_key="k")
    assert isinstance(client, BeadStore)
