#!/usr/bin/env python3
"""Loads the one Python substrate client (``apps/substrate/client/``, M6) by
file path, under a private alias -- never under the name ``substrate_client``
itself.

``scripts/substrate_client.py`` is a different, older client that claims that
same import name. Whichever of the two some other import elsewhere in this
process resolves first would otherwise silently win the ``substrate_client``
entry in ``sys.modules``, handing every later ``from substrate_client import
Substrate`` in this process the wrong class -- exactly the collision
``apps/substrate/client/tests/test_recorder_parity.py`` sidesteps the same
way, for the same reason.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ALIAS = "_factory_dispatcher_shared_substrate_client_pkg"


def _load_substrate_class() -> type:
    module = sys.modules.get(_ALIAS)
    if module is None:
        pkg_init = (
            Path(__file__).resolve().parents[1]
            / "substrate"
            / "client"
            / "src"
            / "substrate_client"
            / "__init__.py"
        )
        spec = importlib.util.spec_from_file_location(
            _ALIAS, pkg_init, submodule_search_locations=[str(pkg_init.parent)]
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[_ALIAS] = module
        spec.loader.exec_module(module)
    return module.Substrate


Substrate = _load_substrate_class()
