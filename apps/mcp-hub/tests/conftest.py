"""Session-wide test configuration.

``tools.factory_status``'s ``REPO_ROOT``/``DISPATCHER_DIR``/``SCRIPTS_DIR``
are explicit configuration (``FACTORY_STATUS_REPO_ROOT``), not derived from
``__file__`` -- the container this service ships in has no repo root to
derive from (see that module's docstring). These tests run from a real
checkout and exercise the real ``factory-dispatcher``/``scripts``/``docs``
trees (``test_factory_status.py``'s ``_load_real_charter``, its ``guards``
comparisons), so the env var is set here, before any test module imports
``factory_status`` -- conftest.py loads ahead of collecting any test module
in this directory.

``tools.task_filing``'s ``FACTORY_DISPATCHER_ROOT`` is a distinct variable
(see that module's docstring for why it must not share
``FACTORY_STATUS_REPO_ROOT``'s optional-in-production semantics) but is the
same repo root value in a real checkout, so it is defaulted here the same
way.
"""

import os
import pathlib

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

os.environ.setdefault("FACTORY_STATUS_REPO_ROOT", str(_REPO_ROOT))
os.environ.setdefault("FACTORY_DISPATCHER_ROOT", str(_REPO_ROOT))
