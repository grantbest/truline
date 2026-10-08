"""The finance namespace's integrity-error mapper -- an extension, not the core.

``create_bead`` runs on every namespace; a duplicate ``plaid_transaction_id``
or a duplicate account (institution, mask) pair is a finance-only DB-level
unique violation (migrations 0002 and 0003's indexes), so the detail
routes.py returns for either belongs here, registered through the same
per-namespace hook ``finance_schemas.py`` uses for its router.

This mapper claims a ``23505`` only when the violated constraint is one it
owns BY NAME -- ``idx_unique_plaid_id`` or ``idx_unique_account_mask``. It
used to also match the bare "duplicate key value violates unique constraint"
phrase every Postgres unique violation carries, regardless of which
constraint fired; ``create_bead`` consults a namespace's mapper for the
constraint text alone, not the write's namespace (see routes.py's own note
on this), so that fallback let ANY unique violation -- including migration
0006's arch ``idx_unique_arch_ref`` index -- be misreported as a plaid
duplicate 409. Measured live 2026-09-17: an arch.observation duplicate ref
read as a plaid-duplicate 409 for three days (finding e806eaed) before the
real defect (EA reconciler filing a duplicate ref) was found.

Imported eagerly from ``routes.py`` itself (not lazily, and not only via
``main.py``'s composition root): ``tests/test_main.py`` calls
``routes._is_plaid_duplicate_error`` directly, without constructing the app,
so registration cannot depend on whichever other test module happened to
import ``src.main`` first in the same interpreter -- the fragility
``tests/test_namespace_schema_registry.py``'s own docstring names.
"""

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError

from .namespace_registry import NAMESPACE_INTEGRITY_ERROR_MAPPERS

# The only two unique indexes finance owns (migrations 0002, 0003) mapped to
# the detail message this mapper returns for each. Named, not phrase-matched
# -- a 23505 on any constraint not listed here is not finance's to claim.
_FINANCE_UNIQUE_CONSTRAINT_MESSAGES = {
    "idx_unique_plaid_id": "Bead with this plaid_transaction_id already exists",
    "idx_unique_account_mask": "Bead with this institution/mask already exists",
}


def _violated_finance_constraint(exc: IntegrityError) -> str | None:
    text = str(getattr(exc, "orig", exc))
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    if sqlstate != "23505":
        return None
    for name in _FINANCE_UNIQUE_CONSTRAINT_MESSAGES:
        if name in text:
            return name
    return None


def _is_plaid_duplicate_error(exc: IntegrityError) -> bool:
    return _violated_finance_constraint(exc) is not None


def _map_finance_integrity_error(exc: IntegrityError) -> HTTPException | None:
    constraint = _violated_finance_constraint(exc)
    if constraint is None:
        return None
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=_FINANCE_UNIQUE_CONSTRAINT_MESSAGES[constraint],
    )


NAMESPACE_INTEGRITY_ERROR_MAPPERS.register("finance", _map_finance_integrity_error)
