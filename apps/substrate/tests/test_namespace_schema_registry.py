"""The finance namespace registers itself; the core never imports it.

``NAMESPACE_TYPE_SCHEMAS`` (schemas.py) used to be a module-level dict literal
that compiled in ``finance``, ``dev`` and ``arch`` together. It is now a
registry the core populates for the namespaces it defines directly (``dev``,
``arch``), and that ``finance_schemas.py`` populates for itself, on import,
with nothing in ``schemas.py`` ever naming a finance symbol.

The same shape now covers three more concerns that used to be embedded
directly in the core (2026-09-12 architecture review, finding F2): the
``/beads/finance/summary`` route and the ``rules.py`` router (registered on
``namespace_registry.NAMESPACE_ROUTERS``), the Plaid duplicate-error mapper
(``namespace_registry.NAMESPACE_INTEGRITY_ERROR_MAPPERS``), and the
encrypted-namespace/plaintext-key policy (``crypto.register_encrypted_namespace``).
Each is registered from finance's own module(s)
(``finance_schemas.py``, ``finance_integrity.py``, ``finance_encryption.py``)
rather than compiled into ``routes.py``, ``main.py`` or ``crypto.py``. Unlike
the schema registry, two of these three cannot wait for ``main.py``'s
composition root: ``tests/test_main.py`` calls
``routes._is_plaid_duplicate_error`` directly (no app construction), and
alembic migrations 0005/0007 import ``crypto.ENCRYPTED_NAMESPACES`` standalone
(``alembic upgrade head`` never imports ``main.py`` — see the Dockerfile). So
``routes.py`` and ``crypto.py`` are each their own composition root for the
hook their own standalone consumers need — see their module comments.

Two properties matter here and neither is provable within a single pytest
process, because import state leaks between tests in one interpreter:

  * "the core imports no finance symbol" is only true of a fresh interpreter,
    since something earlier in the same pytest run (e.g. any test importing
    ``src.main``) may already have pulled ``finance_schemas`` into
    ``sys.modules``.
  * "finance is registered when the server runs" has to be checked by
    constructing the app exactly the way it is deployed
    (``uvicorn src.main:app`` -- see ``apps/substrate/Dockerfile``), not by
    calling a finance-specific validation helper. A prior version of this
    refactor put finance's only registration trigger inside
    ``validate_finance_content``, a function with zero production callers
    (``routes.py`` calls ``validate_bead_content`` directly) -- so on the
    actual server, finance validation silently no-opped. The test below
    imports ``src.main`` -- nothing more -- and checks the registry directly,
    so a regression here fails this test instead of shipping quietly.

Both properties are checked via a fresh ``python -c`` subprocess.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

from src.schemas import NamespaceSchemaRegistry, validate_bead_content

SUBSTRATE_DIR = str(Path(__file__).resolve().parents[1])
SRC_DIR = str(Path(__file__).resolve().parents[1] / "src")


def _run_in_fresh_interpreter(snippet: str, *, cwd: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(snippet)],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_core_module_imports_no_finance_symbol_but_still_validates_dev_and_arch():
    _run_in_fresh_interpreter(
        """
        import sys
        import schemas

        assert "finance_schemas" not in sys.modules, (
            "bare `import schemas` pulled in finance_schemas"
        )

        schemas.validate_dev_content(
            "task",
            {
                "lane": "bug-triage",
                "title": "t",
                "intent": "i",
                "context_refs": [],
                "acceptance": ["WHEN x THE y SHALL z"],
                "verification": {"commands": ["true"]},
                "scope": {"paths": ["x"], "forbidden_paths": [".github/workflows/**"]},
                "risk_class": "structural",
                "budget": {"max_agent_minutes": 1, "max_usd": 1.0, "max_tokens": 1},
            },
        )
        try:
            schemas.validate_dev_content("task", {"lane": "not-a-real-lane"})
        except Exception:
            pass
        else:
            raise AssertionError("dev.task with an illegal lane should have been rejected")

        schemas.validate_arch_content(
            "capability",
            {
                "ref": "bc.x",
                "name": "n",
                "description": "d",
                "layer": "demand",
                "owner": "o",
                "evidence": ["e"],
                "assessed_at": "2026-01-01",
                "maturity": "absent",
            },
        )
        try:
            schemas.validate_arch_content("capability", {"ref": "bc.x"})
        except Exception:
            pass
        else:
            raise AssertionError("arch.capability missing required fields should have been rejected")

        assert "finance_schemas" not in sys.modules, (
            "validating dev/arch content pulled in finance_schemas"
        )
        """,
        cwd=SRC_DIR,
    )


def test_registry_is_empty_of_finance_types_when_only_core_is_imported():
    _run_in_fresh_interpreter(
        """
        import schemas

        assert schemas.NAMESPACE_TYPE_SCHEMAS.types_for("finance") == {}
        assert schemas.NAMESPACE_TYPE_SCHEMAS.get("finance", "account") is None
        """,
        cwd=SRC_DIR,
    )


def test_unregistered_namespace_and_type_remain_pass_through():
    # Exactly as validate_bead_content documents: an unknown namespace or an
    # unknown type inside a known namespace is a no-op, not a rejection.
    validate_bead_content("no-such-namespace", "whatever", {"anything": True})
    validate_bead_content("dev", "no-such-type", {"anything": True})


def test_registry_silently_overwrites_a_duplicate_registration():
    # A module-level dict literal has always let a later assignment replace
    # an earlier one. The registry that replaces it keeps that behaviour --
    # refusing a duplicate would be new behaviour, and this change is
    # structural.
    registry = NamespaceSchemaRegistry()
    from src.schemas import DevTaskContent, DevNoteContent

    registry.register("dev", "task", DevTaskContent)
    registry.register("dev", "task", DevNoteContent)
    assert registry.get("dev", "task") is DevNoteContent


def test_finance_is_actually_registered_once_its_module_is_imported():
    # The other half of the extension story: finance is real, just not part
    # of the core. Importing its module is what makes it known here.
    from src import finance_schemas
    from src.schemas import NAMESPACE_TYPE_SCHEMAS

    assert (
        NAMESPACE_TYPE_SCHEMAS.get("finance", "account")
        is finance_schemas.FinanceAccountContent
    )


def test_finance_registers_via_the_apps_composition_root_not_a_dead_function():
    """AC-3: the probe exercises the production entry point.

    ``uvicorn src.main:app`` is how the server is actually started (see
    ``apps/substrate/Dockerfile``). Importing ``src.main`` -- and nothing
    finance-specific -- must be enough for a malformed finance payload to be
    rejected, because that import chain (main -> routes -> finance_schemas)
    is what runs before the server answers its first request.
    """
    _run_in_fresh_interpreter(
        """
        import sys
        import src.main  # exactly how the deployed server is built

        from src import schemas
        from pydantic import ValidationError

        assert schemas.NAMESPACE_TYPE_SCHEMAS.get("finance", "transaction") is not None, (
            "constructing the app the way the server does did not register "
            "finance -- registration must not depend on an incidental import"
        )

        try:
            schemas.validate_bead_content(
                "finance", "transaction", {"iso_currency_code": "USD"}
            )
        except ValidationError:
            pass
        else:
            raise AssertionError(
                "malformed finance/transaction content was accepted when the "
                "app was constructed the way it is deployed"
            )
        """,
        cwd=SUBSTRATE_DIR,
    )


def test_finance_router_and_integrity_mapper_register_from_routes_alone():
    """AC-3's router/mapper half: the entry point their own tests actually use.

    ``tests/test_routes_offload.py`` and ``tests/test_main.py`` both reach
    finance's router and integrity-error mapper through ``src.routes`` alone
    -- a bare ``FastAPI()`` with only ``routes.router`` included, or a direct
    call to ``routes._is_plaid_duplicate_error`` -- never through
    ``src.main``. So importing ``src.routes`` -- nothing more -- must be
    enough, in a fresh interpreter, for both hooks to be populated.
    """
    _run_in_fresh_interpreter(
        """
        from fastapi import FastAPI
        from src import routes
        from src.namespace_registry import (
            NAMESPACE_ROUTERS,
            NAMESPACE_INTEGRITY_ERROR_MAPPERS,
        )

        assert NAMESPACE_ROUTERS.for_namespace("finance"), (
            "importing routes did not register finance's router(s)"
        )
        assert NAMESPACE_INTEGRITY_ERROR_MAPPERS.get("finance") is not None, (
            "importing routes did not register finance's integrity-error mapper"
        )

        # Check via the same mechanism FastAPI itself resolves nested routers
        # through (app.openapi()), not by walking .routes directly -- a
        # router included via include_router is wrapped, not flattened, so
        # a bare `r.path for r in routes.router.routes` would not see it
        # even though routing and the OpenAPI document both do.
        probe = FastAPI()
        probe.include_router(routes.router)
        assert "/beads/finance/summary" in probe.openapi()["paths"], (
            "finance's router was registered but never nested into routes.router, "
            "so app.include_router(routes.router) alone would not mount it"
        )
        """,
        cwd=SUBSTRATE_DIR,
    )


def test_finance_router_mounts_regardless_of_which_side_of_the_cycle_imports_first():
    """Regression: routes.py and finance_schemas.py import each other.

    Python resolves an import cycle differently depending on which module a
    caller reaches first -- the second one to start sees the first one's
    still-incomplete module object. A first version of this hook read
    NAMESPACE_ROUTERS from routes.py's own import tail, which worked when
    routes.py was imported first (the common case, e.g. main.py or most
    tests) but silently mounted nothing when something imported
    finance_schemas directly first, as this test does.
    """
    _run_in_fresh_interpreter(
        """
        from fastapi import FastAPI
        from src import finance_schemas  # the entry point this time, not routes
        from src import routes

        probe = FastAPI()
        probe.include_router(routes.router)
        assert "/beads/finance/summary" in probe.openapi()["paths"]
        assert "/rules/dry-run" in probe.openapi()["paths"]
        """,
        cwd=SUBSTRATE_DIR,
    )


def test_rules_module_imports_standalone_despite_the_routes_finance_schemas_cycle():
    """Regression: entering the import graph at ``src.rules`` used to fail,
    then used to silently drop a route once it stopped failing.

    ``routes.py`` (bottom) used to import ``finance_schemas``, which imported
    ``rules`` to register its router on ``rules``'s behalf -- closing a
    three-module cycle (routes -> finance_schemas -> rules -> routes) that a
    fresh ``import src.rules`` walked into from the wrong side: entering
    there left ``src.rules`` paused partway through its own top -- before
    ``router`` was defined -- so ``finance_schemas``'s attempt to import
    ``router`` from it raised a circular-import ``ImportError``, and
    finance_schemas's bare ``except ImportError`` rewrote that into a
    misleading "no module named 'routes'". ``rules.py`` now registers its
    own router from its own bottom (like ``finance_integrity.py`` /
    ``finance_encryption.py``), so nothing importing it depends on
    ``finance_schemas`` at all.

    That fix relocated the fragility instead of eliminating it: entering at
    ``src.rules`` left routes.py's bottom-of-module ``finance_schemas``
    import (and its ``mount_pending`` call) running BEFORE this module's own
    ``register()`` call, so the resulting ``routes.router`` nested finance's
    summary router but never revisited itself for ``rules``' -- asserting
    only that the import doesn't raise passed while ``/rules/dry-run`` was
    silently absent from the composed app. Assert the surface, not just the
    import: build the app exactly as it is deployed and check both finance
    routes are mounted regardless of which module started the import.
    """
    _run_in_fresh_interpreter(
        """
        import src.rules  # the entry point this time, not routes

        from src.main import app

        # app.openapi() -- not app.routes -- because the installed
        # Starlette version defers include_router()'d sub-routes behind an
        # internal wrapper that app.routes does not flatten; the generated
        # OpenAPI schema (what scripts/generate_openapi.py and M1's byte-
        # identity check both use) is the ground truth for what is actually
        # mounted, and matches this module's own docstring's emphasis on
        # constructing the app exactly as it is deployed.
        paths = sorted(app.openapi()["paths"].keys())
        assert "/rules/dry-run" in paths, (
            "entering the import graph at src.rules dropped /rules/dry-run "
            "from the composed app: " + repr(paths)
        )
        assert "/beads/finance/summary" in paths, (
            "entering the import graph at src.rules dropped "
            "/beads/finance/summary from the composed app: " + repr(paths)
        )
        """,
        cwd=SUBSTRATE_DIR,
    )


def test_encrypted_namespaces_registered_after_routes_import_are_visible_to_routes():
    """Regression: routes.py used to bind ``ENCRYPTED_NAMESPACES`` by value.

    ``crypto.register_encrypted_namespace`` REBINDS the module-level
    ``ENCRYPTED_NAMESPACES`` name rather than mutating it in place; a
    ``from .crypto import ENCRYPTED_NAMESPACES`` in ``routes.py`` froze
    whatever the set was at ``routes.py``'s own import time instead of
    tracking that rebinding. Only finance registering itself from
    ``crypto.py``'s own bottom -- before anything else ever imports
    ``routes.py`` -- hid this: a namespace registered any later would be
    invisible to ``routes.py``'s link-content-inheritance check, silently
    writing that namespace's link content in plaintext. ``routes.py`` now
    reads ``crypto.ENCRYPTED_NAMESPACES`` off the module itself, at call
    time, instead of a name bound once at import.
    """
    _run_in_fresh_interpreter(
        """
        from src import routes

        assert not hasattr(routes, "ENCRYPTED_NAMESPACES"), (
            "routes.py still binds ENCRYPTED_NAMESPACES by value at import "
            "time; a namespace registered after this import would be "
            "invisible to routes.py's own encryption-inheritance check"
        )

        assert "late" not in routes.crypto.ENCRYPTED_NAMESPACES
        routes.crypto.register_encrypted_namespace("late", plaintext_keys=frozenset())
        assert "late" in routes.crypto.ENCRYPTED_NAMESPACES, (
            "a namespace registered after routes.py was imported is not "
            "visible through routes.py's own reference to crypto's live set"
        )
        """,
        cwd=SUBSTRATE_DIR,
    )


def test_encrypted_namespace_registers_before_any_app_composition_root():
    """The encryption-policy half: migrations need this without main.py.

    Alembic migrations 0005 and 0007 do
    ``from src.crypto import ENCRYPTED_NAMESPACES`` standalone -- the
    Dockerfile runs ``alembic upgrade head`` as its own process, before
    ``uvicorn src.main:app`` ever starts -- so a bare ``import crypto`` must
    already carry finance's registration, in a fresh interpreter, with
    nothing about ``src.main`` in the loop.
    """
    _run_in_fresh_interpreter(
        """
        from src import crypto

        assert "finance" in crypto.ENCRYPTED_NAMESPACES, (
            "bare `from src import crypto` did not carry finance's "
            "encryption policy -- migrations 0005/0007 depend on this "
            "without ever importing src.main"
        )
        assert "plaid_transaction_id" in crypto.PLAINTEXT_KEYS
        """,
        cwd=SUBSTRATE_DIR,
    )
