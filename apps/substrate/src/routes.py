import asyncio
import logging
import re
import secrets
import os
import json
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import UUID4, ValidationError
from sqlalchemy import delete as sqla_delete, or_, update as sqla_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession

from .bead_rules import STATE_MACHINES, STATE_MACHINE_ENTRY_STATES
from . import crypto
from .crypto import decrypt_jsonb, encrypt_jsonb
from .database import get_db
from .models import Bead, BeadEvent, BeadLink
from .namespace_registry import NAMESPACE_INTEGRITY_ERROR_MAPPERS

from .schemas import (
    BeadRead,
    BeadCreate,
    BeadUpdate,
    BeadTransition,
    BeadSearchRequest,
    BeadSearchHit,
    BeadEventRead,
    BeadLinkCreate,
    BeadLinkRead,
    check_source_class_admission,
    check_source_class_ownership,
    content_declares_source_class,
    is_automated_writer,
    read_source_class,
    validate_bead_content,
)
from .vector import EmbeddingProviderRetiredError, search_similar_beads
from .cache import get_cached_beads, set_cached_beads, invalidate_cache

# Composition root for this module's own namespace hook: registers finance's
# integrity-error mapper on NAMESPACE_INTEGRITY_ERROR_MAPPERS. Imported here
# rather than left to main.py's composition root because
# tests/test_main.py calls _is_plaid_duplicate_error directly without
# constructing the app -- see finance_integrity.py's module docstring.
from . import finance_integrity  # noqa: F401

logger = logging.getLogger(__name__)
router = APIRouter()

_warned_degenerate_read_key = False

def require_api_key(
    request: Request, x_api_key: Optional[str] = Header(default=None)
) -> None:
    """One key that can only write, and one that can only read.

    ``SUBSTRATE_READ_API_KEY`` is optional and additive: every route in this
    module reaches its authority through this single dependency (no
    per-route copies), so the read key is checked here rather than in a
    sibling dependency the GET routes would have to opt into individually.
    Leaving it unset reproduces today's behaviour byte-for-byte, since the
    read-key branch below is then never entered and the write-key check
    runs exactly as it always has.

    A read key that is blank/whitespace-only, identical to the write key,
    or contains a non-ASCII character is treated as unset rather than
    honoured. The blank/identical cases would otherwise intercept every
    request -- including writes -- before the write-key check ever runs, so
    pasting the same secret into both env vars (the likeliest provisioning
    slip, since they sit side by side wherever they're provisioned) would
    silently turn the substrate write-nothing while every write 403s as
    "read-only key cannot write". The non-ASCII case is different in kind:
    ``secrets.compare_digest`` raises ``TypeError`` on a non-ASCII operand,
    so a read key with one stray smart quote or accented character would
    otherwise crash this dependency on every request -- reads and writes
    alike -- while ``GET /health`` carries no dependency and keeps
    returning 200, so liveness probes read green through a total outage.
    """
    global _warned_degenerate_read_key
    expected_api_key = os.environ.get("SUBSTRATE_API_KEY")
    if not expected_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Substrate API key is not configured",
        )

    expected_read_api_key = os.environ.get("SUBSTRATE_READ_API_KEY")
    if expected_read_api_key and (
        not expected_read_api_key.strip()
        or not expected_read_api_key.isascii()
        or secrets.compare_digest(expected_read_api_key, expected_api_key)
    ):
        if not _warned_degenerate_read_key:
            logger.warning(
                "SUBSTRATE_READ_API_KEY is blank/whitespace-only, "
                "non-ASCII, or identical to SUBSTRATE_API_KEY; ignoring it "
                "so writes are not silently disabled."
            )
            _warned_degenerate_read_key = True
        expected_read_api_key = None

    if (
        expected_read_api_key
        and x_api_key
        and secrets.compare_digest(x_api_key, expected_read_api_key)
    ):
        if request.method != "GET":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="read-only key cannot write",
            )
        return

    if not x_api_key or not secrets.compare_digest(x_api_key, expected_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )

def _bead_to_read(bead: Bead) -> BeadRead:
    return BeadRead(
        id=bead.id,
        namespace=bead.namespace,
        type=bead.type,
        state=bead.state,
        parent_id=bead.parent_id,
        context=decrypt_jsonb(bead.context),
        content=decrypt_jsonb(bead.content),
        confidence=bead.confidence,
        trust_tier=bead.trust_tier,
        provenance=bead.provenance or {},
        created_by=bead.created_by,
        created_at=bead.created_at,
        updated_at=bead.updated_at,
    )

def _link_to_read(link: BeadLink) -> BeadLinkRead:
    return BeadLinkRead(
        id=link.id,
        source_id=link.source_id,
        target_id=link.target_id,
        link_type=link.link_type,
        content=decrypt_jsonb(link.content),
        created_at=link.created_at,
        created_by=link.created_by,
    )

def _find_mapped_integrity_error(
    exc: IntegrityError, namespace: Optional[str] = None
) -> Optional[HTTPException]:
    """The verdict of the mapper registered for ``namespace`` on ``exc``, if any.

    Namespace-SCOPED: a mapper only ever sees an ``IntegrityError`` raised
    while writing to the namespace it is registered for. Before this, every
    registered mapper was consulted for every write regardless of that
    write's own namespace, relying solely on the constraint-name check
    inside the mapper to decide relevance -- and the finance mapper used to
    match on the generic phrase every Postgres unique violation carries, so
    an unrelated namespace's unique-constraint failure (e.g. arch's
    ``idx_unique_arch_ref``, migration 0006) was misreported as a plaid
    duplicate (finding e806eaed). ``namespace=None`` preserves the old
    all-mappers behaviour for callers that only need the classifier logic,
    not the dispatch (``_is_plaid_duplicate_error``, below).
    """
    if namespace is not None:
        mapper = NAMESPACE_INTEGRITY_ERROR_MAPPERS.get(namespace)
        mappers = [mapper] if mapper is not None else []
    else:
        mappers = NAMESPACE_INTEGRITY_ERROR_MAPPERS.all()
    for mapper in mappers:
        mapped = mapper(exc)
        if mapped is not None:
            return mapped
    return None


def _is_plaid_duplicate_error(exc: IntegrityError) -> bool:
    """Whether a namespace-registered integrity-error mapper claims ``exc``.

    Kept as a boolean predicate for callers that only need the yes/no (see
    ``tests/test_main.py``) — the mapped ``HTTPException`` itself, not this
    function, is what ``create_bead`` raises, so a namespace owns its own
    error wording without the core repeating it (today the only registrant
    is ``finance``; see ``finance_integrity.py``). Namespace-unscoped: this
    checks only whether the error text names a constraint some mapper owns,
    not whether it was raised for that mapper's own namespace -- the
    dispatch in ``create_bead`` applies the namespace scope itself.
    """
    return _find_mapped_integrity_error(exc) is not None


def _violated_unique_constraint_name(exc: IntegrityError) -> Optional[str]:
    """The quoted constraint name Postgres names in a 23505's own text, if any.

    Used only as the namespace-neutral fallback in ``create_bead``: once no
    namespace-scoped mapper and neither of the two named 23505 checks above
    claim ``exc``, the caller still deserves to know WHICH constraint fired
    rather than an unhandled 500 or a wrong namespace's canned message.
    """
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    if sqlstate != "23505":
        return None
    text = str(getattr(exc, "orig", exc))
    match = re.search(r'unique constraint "?([A-Za-z0-9_]+)"?', text)
    return match.group(1) if match else None

SPEC_IDENTITY_INDEX_NAME = "idx_unique_dev_task_spec_identity_live"


def _is_spec_identity_duplicate_error(exc: IntegrityError) -> bool:
    """Whether ``exc`` is migration 0008's partial unique index firing —
    OPS-68's fix for two concurrent ``dev.task`` filings of the same
    ``content.spec_identity``.

    Named-constraint check, like the finance mapper's own (see
    ``finance_integrity.py``) -- neither reads the other's index name, so
    the two cannot misclassify one another's violation regardless of check
    order.
    """
    text = str(getattr(exc, "orig", exc))
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    return sqlstate == "23505" and SPEC_IDENTITY_INDEX_NAME in text

def _is_missing_parent_error(exc: IntegrityError) -> bool:
    """Whether ``exc`` is ``bead.parent_id``'s self-FK firing because
    ``parent_id`` names a bead that does not exist.

    SQLSTATE 23503 (foreign key violation), not 23505 (unique violation) —
    disjoint from ``_is_plaid_duplicate_error`` and
    ``_is_spec_identity_duplicate_error``, so this can be checked in any
    order relative to them without risk of misclassifying a duplicate as a
    missing parent or vice versa.

    ``bead_event.bead_id``, inserted in the same transaction as the bead
    row, always names the id ``create_bead`` just flushed, so ``parent_id``
    is the only FK a create can violate.
    """
    text = str(getattr(exc, "orig", exc))
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    return sqlstate == "23503" and "parent_id" in text


def _serializable_errors(exc: ValidationError) -> list:
    """Render pydantic errors as JSON-safe dicts.

    ``ValidationError.errors()`` puts the original exception OBJECT in each
    entry's ``ctx`` when a validator raises ``ValueError`` — which every
    custom validator in schemas.py does. FastAPI then cannot serialize the
    422 body and the request fails with a 500 instead, hiding the real
    (client-side) error behind an apparent server fault.

    Seen in production 2026-07-27: every ``dev.note`` cross-field violation
    returned 500. Keep the message, drop the unserializable object.
    """
    cleaned = []
    for err in exc.errors(include_url=False):
        entry = dict(err)
        ctx = entry.get("ctx")
        if isinstance(ctx, dict):
            entry["ctx"] = {k: str(v) for k, v in ctx.items()}
        elif ctx is not None:
            entry["ctx"] = str(ctx)
        cleaned.append(entry)
    return cleaned


def _validate_content_or_422(namespace: str, bead_type: str, content: dict) -> None:
    try:
        validate_bead_content(namespace, bead_type, content)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": f"{namespace}_content_schema_violation",
                "bead_type": bead_type,
                "errors": _serializable_errors(exc),
            },
        ) from exc


def _validate_state_transition_or_422(
    namespace: str,
    bead_type: str,
    from_state: str,
    to_state: str,
    bead_id: Optional[str] = None,
) -> None:
    machine = STATE_MACHINES.get((namespace, bead_type))
    if machine is None:
        return

    allowed = machine.get(from_state, frozenset())
    if to_state not in allowed:
        detail = {
            "error": "illegal_transition",
            "machine": f"{namespace}.{bead_type}",
            "from_state": from_state,
            "to_state": to_state,
            "allowed": sorted(allowed),
        }
        if bead_id is not None:
            detail["bead_id"] = bead_id
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=detail,
        )


def _reject_source_class_admission_violation_or_409(content: dict, writer: str) -> None:
    """Refuse a write that claims a source_class ``writer`` isn't enrolled for.

    OPS-86: fires where the ownership check (:func:`_reject_source_class_write_violation_or_409`)
    cannot — either no existing bead owns this ``ref`` (a fresh create) or the
    existing fact is being RELABELED into a different class (an overwrite that
    also mints a new claim). Without this, any human-exempt writer could mint
    a fresh "observed" or "derived" fact merely by declaring the class in
    content, or launder an unenrolled claim through two requests: an authored
    POST (open to any human writer) followed by a class-changing PATCH that
    ownership alone waves through.

    This is the ONE admission gate — called directly for a fresh ref/create,
    and again from :func:`_reject_source_class_write_violation_or_409` for a
    class-changing overwrite — consuming the same declared map
    (``bead_rules.SOURCE_CLASS_WRITERS``) either way. No second implementation.
    """
    declared_class = read_source_class(content)
    violation = check_source_class_admission(declared_class, writer)
    if violation is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "source_class_admission_violation",
                "declared_class": violation.owning_class,
                "rejected_writer": violation.rejected_writer,
                "enrollment_path": "apps/substrate/src/bead_rules.py:SOURCE_CLASS_WRITERS",
            },
        )


def _reject_undeclared_source_class_rewrite_by_automated_writer_or_409(
    bead: Bead, existing_content: dict, writer: str
) -> None:
    """Refuse an automated writer's rewrite of a bead that never declared its
    own ``source_class``, with a detail distinct from an ownership violation.

    ``read_source_class`` defaults a missing/unrecognized stored value to
    ``"authored"`` so historical content stays valid — but that default also
    makes an undeclared standing bead indistinguishable, to the ownership
    check alone, from a fact a human genuinely declared ``authored``. D8
    (2026-09-13 decision record) named exactly this conflation: making every
    ``factory-dispatcher/*`` writer automated (the correct predicate fix, see
    :func:`is_automated_writer`) then 409'd a reconciler's own class-preserving
    rewrite of its undeclared standing bead with the same detail a real
    authored-fact violation gets — a refusal with nothing for the reconciler
    to act on. This fires first and names the missing field instead, so the
    caller learns the bead must be declared before it can be rewritten by
    automation, rather than reading a permanent lock into a temporary gap.
    The ownership rule itself is untouched: a REAL authored fact (declared
    explicitly, not defaulted) still falls through to the ownership check
    below and is refused exactly as before.

    A human writer is unaffected either way — ownership already lets a human
    overwrite an undeclared (defaulted-authored) fact, and this check only
    ever fires for an automated writer.
    """
    if is_automated_writer(writer) and not content_declares_source_class(existing_content):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "source_class_declaration_required",
                "bead_id": str(bead.id),
                "missing_field": "source_class",
                "message": (
                    "this bead must be declared with a source_class before "
                    "an automated writer may rewrite it"
                ),
                "rejected_writer": writer,
            },
        )


def _reject_source_class_write_violation_or_409(
    bead: Bead, new_content: dict, writer: str
) -> None:
    """Refuse a content overwrite that crosses the arch reconciliation contract.

    Only ``arch`` beads carry a ``source_class``; the caller gates this on
    namespace. Two checks, run in sequence:

    1. Ownership — may ``writer`` overwrite the *existing* fact at all, per
       :func:`check_source_class_ownership`. Relabeling ``source_class`` itself
       is still an overwrite of the existing fact and goes through this gate
       like any other content change.
    2. Admission — only when the write would CHANGE ``source_class``, is
       ``writer`` enrolled for the class it is newly claiming? A relabel is a
       mint-in-place: the same claim a fresh POST makes, so it re-runs the
       identical :func:`_reject_source_class_admission_violation_or_409` gate
       that a fresh POST runs, rather than a second copy of it. A
       class-preserving write skips this: ownership alone already answered
       whether this writer may touch the fact, and no new class is being
       claimed for admission to police.

    This closes the laundering gap an OPS-86 audit named: an authored POST is
    open to any non-automated writer (the human exemption), and — before this
    check — a follow-up PATCH that relabeled that same fact's class only ever
    ran ownership, which the authored exemption passes for any non-automated
    writer regardless of what class it was relabeling to. Two requests could
    mint a class no single request could have claimed at POST.

    Called from both ``PATCH`` (``bead`` is the bead being edited) and
    ``POST`` (``bead`` is an existing bead a new create's ``content.ref`` already
    names — see ``_find_existing_arch_bead_by_ref``): a create that names an
    already-owned fact is an overwrite of it wearing a different verb.
    """
    existing_content = decrypt_jsonb(bead.content)
    _reject_undeclared_source_class_rewrite_by_automated_writer_or_409(
        bead, existing_content, writer
    )
    existing_class = read_source_class(existing_content)
    violation = check_source_class_ownership(existing_class, bead.created_by, writer)
    if violation is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "source_class_ownership_violation",
                "bead_id": str(bead.id),
                "owning_class": violation.owning_class,
                "rejected_writer": violation.rejected_writer,
            },
        )

    new_class = read_source_class(new_content)
    if new_class != existing_class:
        _reject_source_class_admission_violation_or_409(new_content, writer)


async def _find_existing_arch_bead_by_ref(db: AsyncSession, ref: str) -> Optional[Bead]:
    """The existing arch bead this ``ref`` already identifies, if any.

    ``content.ref`` is the reconciliation contract's identity key (migration 0006's
    namespace-wide unique partial index) — the same key ``list_beads``' ``content_ref``
    filter already queries. Namespace-scoped, not type-scoped, matching that index's
    own scope.
    """
    result = await db.execute(
        select(Bead).filter(
            Bead.namespace == "arch", Bead.content.op("->>")("ref") == ref
        )
    )
    return result.scalar_one_or_none()


def _validate_entry_state_or_422(namespace: str, bead_type: str, state: str) -> None:
    """Reject a create whose initial state is not a declared entry state.

    A declared machine says which edges are legal between existing states;
    creation has no from_state, so it needs its own list of states a bead is
    allowed to be born into. A pair with no declared machine stays permissive,
    matching every other STATE_MACHINES consult in this module.
    """
    machine = STATE_MACHINES.get((namespace, bead_type))
    if machine is None:
        return

    entry_states = STATE_MACHINE_ENTRY_STATES.get((namespace, bead_type), frozenset())
    if state not in entry_states:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": "illegal_entry_state",
                "machine": f"{namespace}.{bead_type}",
                "state": state,
                "allowed": sorted(entry_states),
            },
        )

@router.post("/beads", response_model=BeadRead, dependencies=[Depends(require_api_key)])
async def create_bead(bead: BeadCreate, db: AsyncSession = Depends(get_db)):
    # Schema-on-write for hardened bead content contracts. Run BEFORE
    # encryption so validators see plaintext content. Unknown
    # namespace/type pairs pass through until they have explicit models.
    _validate_content_or_422(bead.namespace, bead.type, bead.content)

    # A create is a state-changing write too: it assigns the bead's first
    # state without ever consulting a from_state. For a declared machine,
    # that first state must be one of its declared entry states, or a
    # creator could hand a fresh bead a state no legal edge ever produces.
    _validate_entry_state_or_422(bead.namespace, bead.type, bead.state)

    # The reconciliation contract's write-violation check (PATCH, below) only
    # ever fired on an edit to a bead already on hand. A create names its own
    # content.ref — the contract's identity half (migration 0006) — so a POST
    # that names a ref an existing bead already owns is an overwrite of that
    # fact wearing a different verb, and gets the same ownership+admission
    # check before it ever reaches the database, not after a duplicate row it
    # can no longer catch. That check also re-runs admission if this POST
    # relabels the class (OPS-86 laundering gap).
    #
    # A fresh ref (or no ref at all) has no existing bead to run that check
    # against at all — OPS-86's original vector, since nothing then said who
    # may CLAIM a source_class in the first place. The admission gate is that
    # rule, run directly here for a fresh mint.
    if bead.namespace == "arch":
        ref = (bead.content or {}).get("ref")
        existing = await _find_existing_arch_bead_by_ref(db, ref) if ref else None
        if existing is not None:
            _reject_source_class_write_violation_or_409(
                existing, bead.content, bead.created_by
            )
        else:
            _reject_source_class_admission_violation_or_409(bead.content, bead.created_by)

    plaintext_payload = bead.model_dump(mode="json")

    fields = bead.model_dump()
    fields["content"] = encrypt_jsonb(fields.get("content"), bead.namespace)
    fields["context"] = encrypt_jsonb(fields.get("context"), bead.namespace)
    new_bead = Bead(**fields)

    try:
        db.add(new_bead)
        await db.flush()

        event_payload = dict(plaintext_payload)
        event_payload["content"] = encrypt_jsonb(plaintext_payload.get("content"), bead.namespace)
        event_payload["context"] = encrypt_jsonb(plaintext_payload.get("context"), bead.namespace)
        
        # Ensure UUIDs are strings in JSONB event payload
        event_payload = json.loads(json.dumps(event_payload, default=str))

        event = BeadEvent(
            bead_id=new_bead.id,
            event_type="created",
            to_state=new_bead.state,
            payload=event_payload,
            created_by=new_bead.created_by,
        )
        db.add(event)
        await db.commit()
        await db.refresh(new_bead)
        
        # Phase 8 Architect: Invalidate cache on write
        await invalidate_cache(bead.namespace)
    except IntegrityError as exc:
        await db.rollback()
        if _is_spec_identity_duplicate_error(exc):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "spec_identity_duplicate",
                    "spec_identity": (bead.content or {}).get("spec_identity"),
                },
            ) from exc
        mapped = _find_mapped_integrity_error(exc, bead.namespace)
        if mapped is not None:
            raise mapped from exc
        if _is_missing_parent_error(exc):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "parent_not_found",
                    "parent_id": str(bead.parent_id),
                },
            ) from exc
        constraint = _violated_unique_constraint_name(exc)
        if constraint is not None:
            # No namespace-scoped mapper claimed this 23505 (or none is
            # registered for bead.namespace) -- a namespace-neutral 409
            # naming the constraint that actually fired, not another
            # namespace's canned message and not an unhandled 500.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "unique_constraint_violation",
                    "constraint": constraint,
                },
            ) from exc
        raise
    except Exception:
        await db.rollback()
        raise

    return _bead_to_read(new_bead)

@router.patch("/beads/{bead_id}", response_model=BeadRead, dependencies=[Depends(require_api_key)])
async def update_bead(
    bead_id: UUID4, 
    update: BeadUpdate, 
    to_state: Optional[str] = None, # Support legacy query param
    created_by: str = "system", # Support legacy query param
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(Bead).filter(Bead.id == bead_id))
    bead = result.scalar_one_or_none()
    if not bead:
        raise HTTPException(status_code=404, detail="Bead not found")
    
    from_state = bead.state
    updater = update.created_by or created_by

    # Handle both legacy query params and new body
    target_state = to_state or update.state
    if target_state:
        _validate_state_transition_or_422(
            bead.namespace,
            bead.type,
            from_state,
            target_state,
            bead_id=str(bead_id),
        )
        bead.state = target_state

    if update.parent_id is not None:
        bead.parent_id = update.parent_id

    if update.content is not None:
        _validate_content_or_422(bead.namespace, bead.type, update.content)
        if bead.namespace == "arch":
            _reject_source_class_write_violation_or_409(bead, update.content, updater)
        bead.content = encrypt_jsonb(update.content, bead.namespace)

    if update.context is not None:
        bead.context = encrypt_jsonb(update.context, bead.namespace)
        
    if update.confidence is not None:
        bead.confidence = update.confidence

    # Record update event. Mirrors create_bead's event encryption: only the
    # fields actually present in this update are run through encrypt_jsonb
    # (encrypt_jsonb is a no-op outside ENCRYPTED_NAMESPACES), so the event
    # payload never regresses to storing plaintext content/context for an
    # encrypted namespace the way a raw model_dump() would.
    event_payload = update.model_dump()
    if update.content is not None:
        event_payload["content"] = encrypt_jsonb(update.content, bead.namespace)
    if update.context is not None:
        event_payload["context"] = encrypt_jsonb(update.context, bead.namespace)
    serializable_payload = json.loads(json.dumps(event_payload, default=str))
    event = BeadEvent(
        bead_id=bead.id,
        event_type="updated",
        from_state=from_state,
        to_state=bead.state,
        payload=serializable_payload,
        created_by=updater,
    )
    db.add(event)
    try:
        await db.commit()
        await db.refresh(bead)

        # Phase 8 Architect: Invalidate cache on update
        await invalidate_cache(bead.namespace)

    except IntegrityError as exc:
        await db.rollback()
        # Same self-FK violation create_bead classifies (23503 on
        # parent_id) -- reuses that classifier rather than a second
        # implementation of the contract. update.parent_id, not
        # bead.parent_id: it's the caller-supplied id that violated the
        # FK, and it's only ever set when this branch can fire (see the
        # `if update.parent_id is not None` assignment above).
        if _is_missing_parent_error(exc):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "parent_not_found",
                    "parent_id": str(update.parent_id),
                },
            ) from exc
        raise
    except Exception:
        await db.rollback()
        raise
    return _bead_to_read(bead)


# STATE_MACHINES and STATE_MACHINE_ENTRY_STATES — the legal edges named in
# ARCHITECTURE.md §3.1 — live in .bead_rules (imported above) so callers that
# are not this web app (e.g. the factory dispatcher) can enforce the same
# dev.task lifecycle and arch.release lifecycle without importing FastAPI or
# the database layer.


@router.post("/beads/{bead_id}/transition", response_model=BeadRead, dependencies=[Depends(require_api_key)])
async def transition_bead(
    bead_id: UUID4,
    req: BeadTransition,
    db: AsyncSession = Depends(get_db),
):
    """Compare-and-set state transition — ARCHITECTURE.md §3.1's ``transition``.

    This is the **claim primitive**. The factory dispatcher is single-runner for
    exactly one reason: without a compare-and-set, claiming is a read-then-write
    and two runners can both claim the same task. So the swap here is a single
    conditional ``UPDATE ... WHERE id = :id AND state = :from_state``, not a
    read-check-assign — under PostgreSQL's default READ COMMITTED the latter
    leaves a gap in which both callers read ``pending``, both pass the check,
    and both write. Getting that wrong is undetectable in a single-runner test
    and is the whole point of the endpoint, so it is written as one statement
    and asserted concurrently in ``test_transition.py``.

    ``from_state`` is required rather than optional: an unconditional path here
    would be indistinguishable from ``PATCH``, which already exists for callers
    that genuinely do not care.

    Returns 404 if the bead is gone, 422 if the edge is illegal for the bead's
    declared state machine, 409 if the bead has moved on since the caller read it.
    """
    result = await db.execute(select(Bead).filter(Bead.id == bead_id))
    bead = result.scalar_one_or_none()
    if not bead:
        raise HTTPException(status_code=404, detail="Bead not found")

    # Legality is a property of the declared edge, so it is checked against the
    # requested from_state. Whether the bead is *actually* in that state is the
    # UPDATE's job below — the two questions are separate and get separate codes.
    machine = STATE_MACHINES.get((bead.namespace, bead.type))
    if machine is not None:
        _validate_state_transition_or_422(
            bead.namespace,
            bead.type,
            req.from_state,
            req.to_state,
            bead_id=str(bead_id),
        )

    namespace = bead.namespace
    stmt = (
        sqla_update(Bead)
        .where(Bead.id == bead_id, Bead.state == req.from_state)
        .values(state=req.to_state)
        .returning(Bead.id)
        # The ORM copy fetched above is deliberately not synchronised; it is
        # refreshed after commit instead, so the identity map cannot serve a
        # stale state to the response.
        .execution_options(synchronize_session=False)
    )
    try:
        updated = await db.execute(stmt)
        if updated.scalar_one_or_none() is None:
            await db.rollback()
            # Re-read only on the failure path, and only to say what it lost to.
            current = await db.execute(select(Bead.state).where(Bead.id == bead_id))
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "state_mismatch",
                    "bead_id": str(bead_id),
                    "expected_state": req.from_state,
                    "current_state": current.scalar_one_or_none(),
                },
            )

        db.add(
            BeadEvent(
                bead_id=bead_id,
                event_type="transitioned",
                from_state=req.from_state,
                to_state=req.to_state,
                payload={"from_state": req.from_state, "to_state": req.to_state},
                created_by=req.created_by,
            )
        )
        await db.commit()
    except HTTPException:
        raise
    except IntegrityError as exc:
        await db.rollback()
        # A legal transition INTO a live state (failed->pending requeue,
        # doing/review->pending return) can trip the spec-identity index when
        # the identity was legitimately refiled in the meantime -- a governed
        # 409 naming the conflict, not an unhandled 500 (release-gate
        # advisory on the PR that added the index).
        if _is_spec_identity_duplicate_error(exc):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "spec_identity_conflict_on_transition",
                    "message": (
                        "transitioning this bead into a live state collides "
                        "with a live bead carrying the same spec_identity "
                        "(it was refiled); supersede one of them instead"
                    ),
                },
            ) from exc
        raise
    except Exception:
        await db.rollback()
        raise

    await db.refresh(bead)
    await invalidate_cache(namespace)
    return _bead_to_read(bead)


# Single-flight guard for list_beads cache fills, keyed by cache_key.
# Bounded by the prune in list_beads; entries for one-off keys (e.g.
# created_after timestamps) are dropped once idle.
_list_beads_locks: dict[str, asyncio.Lock] = {}

_LIST_BEADS_QUERY_PARAMS = frozenset(
    {
        "namespace",
        "type",
        "state",
        "trust_tier",
        "parent_id",
        "created_after",
        "content_ref",
        "limit",
        "offset",
    }
)


def _reject_unknown_list_beads_params(request: Request | None) -> None:
    if request is None:
        return
    unknown = sorted(set(request.query_params.keys()) - _LIST_BEADS_QUERY_PARAMS)
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "unknown_query_parameter",
                "parameters": unknown,
            },
        )


@router.get("/beads", response_model=List[BeadRead], dependencies=[Depends(require_api_key)])
async def list_beads(
    request: Request = None,
    namespace: Optional[str] = None,
    type: Optional[str] = None,
    state: Optional[str] = None,
    trust_tier: Optional[str] = None,
    parent_id: Optional[str] = None,
    created_after: Optional[datetime] = None,
    content_ref: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db)
):
    _reject_unknown_list_beads_params(request)

    # Phase 8 Architect: Check Redis cache first.
    # Decrypted results are cached to bypass heavy Fernet decryption.
    cache_key = f"beads:{namespace or '*'}:{type or '*'}:{state or '*'}:{trust_tier or '*'}:{parent_id or '*'}:{created_after or '*'}:{limit}:{offset}"
    if content_ref is not None:
        cache_key = f"{cache_key}:content_ref:{content_ref}"
    cached = await get_cached_beads(cache_key)
    if cached:
        return cached

    # Single-flight per cache_key: the five bank-sync schedules fire the
    # identical type=transaction&limit=5000 query in the same second, and
    # concurrent misses each ran the full fetch+decrypt — enough combined
    # CPU that every caller hit its 30s ReadTimeout while staggered runs
    # sailed through (observed 2026-07-18, 5 institutions x 4 retries =
    # 20 failed syncs from one herd). Followers wait for the leader and
    # reread the cache it just filled.
    if len(_list_beads_locks) > 256:
        for stale_key in [k for k, v in _list_beads_locks.items() if not v.locked()]:
            del _list_beads_locks[stale_key]
    lock = _list_beads_locks.setdefault(cache_key, asyncio.Lock())
    async with lock:
        cached = await get_cached_beads(cache_key)
        if cached:
            return cached

        query = select(Bead)
        if namespace:
            query = query.filter(Bead.namespace == namespace)
        if type:
            query = query.filter(Bead.type == type)
        if state:
            query = query.filter(Bead.state == state)
        if trust_tier:
            query = query.filter(Bead.trust_tier == trust_tier)
        if parent_id:
            query = query.filter(Bead.parent_id == parent_id)
        if created_after:
            query = query.filter(Bead.created_at >= created_after)
        if content_ref is not None:
            query = query.filter(Bead.content.op("->>")("ref") == content_ref)

        query = query.order_by(Bead.created_at.desc()).limit(limit).offset(offset)
        result = await db.execute(query)
        rows = result.scalars().all()

        # Decrypt + serialize off-loop in one hop. Bulk Fernet decryption is
        # CPU-bound (and model_dump over thousands of beads is too); inline it
        # pins the loop and starves /health until probes kill the pod —
        # observed live 2026-06-11. Single-bead paths stay inline; the thread
        # hop only pays for itself on batches.
        def _decrypt_and_dump() -> tuple[List[BeadRead], list[dict]]:
            reads = [_bead_to_read(b) for b in rows]
            return reads, [r.model_dump(mode="json") for r in reads]

        beads_read, dumped = await asyncio.to_thread(_decrypt_and_dump)

        # Store in cache as JSON-serializable list.
        await set_cached_beads(cache_key, dumped)

        return beads_read

@router.post(
    "/beads/search",
    response_model=List[BeadSearchHit],
    dependencies=[Depends(require_api_key)],
)
async def search_beads(
    request: BeadSearchRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        hits = await search_similar_beads(
            request.query,
            limit=request.limit,
            namespace=request.namespace,
            type_=request.type,
        )
    except EmbeddingProviderRetiredError as exc:
        # Expected, not an incident: log once at WARNING (no traceback) and
        # hand the caller the same named reason instead of a bare 502.
        logger.warning("Semantic search unavailable: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        )
    except Exception as exc:
        logger.exception("Semantic search failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Vector search unavailable: {type(exc).__name__}",
        )

    if not hits:
        return []

    ids_in_order = [hit["bead_id"] for hit in hits if hit.get("bead_id")]
    if not ids_in_order:
        return []

    result = await db.execute(select(Bead).filter(Bead.id.in_(ids_in_order)))
    beads_by_id = {str(b.id): b for b in result.scalars().all()}

    score_by_id = {hit["bead_id"]: float(hit.get("score") or 0.0) for hit in hits}

    def _rank_and_decrypt() -> List[BeadSearchHit]:
        ranked: List[BeadSearchHit] = []
        for bead_id in ids_in_order:
            bead = beads_by_id.get(bead_id)
            if bead is None:
                logger.warning("Qdrant hit %s missing from Postgres; skipping", bead_id)
                continue
            ranked.append(
                BeadSearchHit(
                    bead=_bead_to_read(bead),
                    score=score_by_id.get(bead_id, 0.0),
                )
            )
        return ranked

    return await asyncio.to_thread(_rank_and_decrypt)


@router.get("/beads/{bead_id}/events", response_model=List[BeadEventRead], dependencies=[Depends(require_api_key)])
async def list_bead_events(bead_id: UUID4, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(BeadEvent).filter(BeadEvent.bead_id == bead_id).order_by(BeadEvent.created_at.asc()))
    events = result.scalars().all()

    def _decrypt_events() -> List[BeadEventRead]:
        return [
            BeadEventRead(
                id=e.id,
                bead_id=e.bead_id,
                event_type=e.event_type,
                from_state=e.from_state,
                to_state=e.to_state,
                payload=decrypt_jsonb(e.payload),
                created_at=e.created_at,
                created_by=e.created_by,
            )
            for e in events
        ]

    return await asyncio.to_thread(_decrypt_events)

@router.get("/beads/{bead_id}", response_model=BeadRead, dependencies=[Depends(require_api_key)])
async def get_bead(bead_id: UUID4, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Bead).filter(Bead.id == bead_id))
    bead = result.scalar_one_or_none()
    if not bead:
        raise HTTPException(status_code=404, detail="Bead not found")
    return _bead_to_read(bead)

# ---------------------------------------------------------------------------
# Links — ARCHITECTURE.md §3.1 `link`, the primitive that was never built.
#
# Edges lived inside `content` until now (dev.note.answers_ref,
# dev.task.source_bead_ids, the EA model's realizes/depends_on). Two reasons
# that had to stop: ALE encrypts content leaves, so a ref there is ciphertext
# and can never be a SQL filter; and a content ref to a deleted bead dangles
# silently. Both FKs cascade, and the semantic columns are plaintext.
# ---------------------------------------------------------------------------

_LINK_DIRECTIONS = ("outgoing", "incoming", "both")


@router.post(
    "/beads/{bead_id}/links",
    response_model=BeadLinkRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_api_key)],
)
async def create_bead_link(
    bead_id: UUID4,
    link: BeadLinkCreate,
    x_created_by: Optional[str] = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    """Link ``bead_id`` (source) to ``link.target_id`` (target)."""
    if bead_id == link.target_id:
        # Also enforced by ck_bead_link_no_self; caught here so the caller
        # gets a 422 describing the problem rather than a 500 from the CHECK.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": "self_link", "bead_id": str(bead_id)},
        )

    # Verify both endpoints before writing. The FKs would catch a missing bead
    # anyway, but as a 409-shaped IntegrityError that cannot say which end was
    # wrong — and "which end" is the only useful part of that error.
    found = await db.execute(
        select(Bead.id, Bead.namespace).where(Bead.id.in_([bead_id, link.target_id]))
    )
    namespaces = {row[0]: row[1] for row in found.all()}
    missing = [str(b) for b in (bead_id, link.target_id) if b not in namespaces]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "bead_not_found", "missing": missing},
        )

    # A link has no namespace of its own, so it inherits the stricter of its two
    # endpoints: if either end is encrypted, the link content is too. A link from
    # a dev task to a finance account can carry financial detail in its content,
    # and source-only inheritance would leave that in plaintext.
    #
    # Read via the `crypto` module reference, not a `from .crypto import
    # ENCRYPTED_NAMESPACES` name bound at import time: `register_encrypted_
    # namespace` rebinds that name on the `crypto` module (it isn't mutated
    # in place), so a by-value import here would freeze whatever was
    # registered by the time this module was first imported and miss any
    # namespace a later-imported module registers as encrypted.
    link_namespace = next(
        (ns for ns in namespaces.values() if ns in crypto.ENCRYPTED_NAMESPACES),
        namespaces[bead_id],
    )

    bead_link = BeadLink(
        source_id=bead_id,
        target_id=link.target_id,
        link_type=link.link_type,
        content=encrypt_jsonb(link.content, link_namespace),
        created_by=x_created_by or "unknown",
    )
    db.add(bead_link)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "duplicate_link",
                "source_id": str(bead_id),
                "target_id": str(link.target_id),
                "link_type": link.link_type,
            },
        ) from exc
    await db.refresh(bead_link)
    return _link_to_read(bead_link)


@router.get(
    "/beads/{bead_id}/links",
    response_model=List[BeadLinkRead],
    dependencies=[Depends(require_api_key)],
)
async def list_bead_links(
    bead_id: UUID4,
    direction: str = "both",
    link_type: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """Traverse edges from ``bead_id``.

    ``direction`` is validated rather than defaulted-on-typo: B-106 is the
    standing lesson that a silently ignored filter is worse than an error,
    because the caller cannot tell "filter applied" from "filter ignored".
    """
    if direction not in _LINK_DIRECTIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": "invalid_direction",
                "given": direction,
                "allowed": list(_LINK_DIRECTIONS),
            },
        )

    query = select(BeadLink)
    if direction == "outgoing":
        query = query.where(BeadLink.source_id == bead_id)
    elif direction == "incoming":
        query = query.where(BeadLink.target_id == bead_id)
    else:
        query = query.where(
            or_(BeadLink.source_id == bead_id, BeadLink.target_id == bead_id)
        )
    if link_type is not None:
        query = query.where(BeadLink.link_type == link_type.strip().lower())

    result = await db.execute(query.order_by(BeadLink.created_at))
    return [_link_to_read(row) for row in result.scalars().all()]


@router.delete("/links/{link_id}", dependencies=[Depends(require_api_key)])
async def delete_bead_link(link_id: UUID4, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(BeadLink).filter(BeadLink.id == link_id))
    bead_link = result.scalar_one_or_none()
    if not bead_link:
        raise HTTPException(status_code=404, detail="Link not found")
    await db.delete(bead_link)
    await db.commit()
    return {"status": "deleted"}


@router.delete("/beads/{bead_id}", dependencies=[Depends(require_api_key)])
async def delete_bead(bead_id: UUID4, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Bead).filter(Bead.id == bead_id))
    bead = result.scalar_one_or_none()
    if not bead:
        raise HTTPException(status_code=404, detail="Bead not found")
    namespace = bead.namespace

    # bead_events.bead_id has a NO ACTION FK — every bead carries at least
    # its "created" event, so the bare delete always violated the FK.
    # Hard-delete semantics take the audit trail with the bead.
    await db.execute(sqla_delete(BeadEvent).where(BeadEvent.bead_id == bead_id))
    await db.delete(bead)
    try:
        await db.commit()
    except IntegrityError as exc:
        # Remaining referrer: another bead points here via parent_id
        # (NO ACTION self-FK). Caller must unlink or delete children first.
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Bead is referenced as parent_id by other beads",
        ) from exc

    # Phase 8 Architect: Invalidate cache on delete
    await invalidate_cache(namespace)

    return {"status": "deleted"}


# Composition root for rules.py's own namespace hook: importing it here
# registers its router on NAMESPACE_ROUTERS from rules.py's own bottom (see
# its comment) rather than depending on finance_schemas.py to import and
# register it -- that used to close a
# routes -> finance_schemas -> rules -> routes import cycle that broke a
# fresh `import src.rules` (entering there left this module paused above,
# before `router` existed, so finance_schemas.py's import of it raised a
# circular-import ImportError). Placed before the finance_schemas import
# below so this router is already registered by the time that import's
# mount_pending() call runs.
#
# Placed at the bottom, not the top: rules.py imports require_api_key and
# list_beads from this module -- both already defined above by the time this
# import runs, which is what lets rules.py import `.routes` back without a
# circular-import failure.
from . import rules  # noqa: E402,F401

# Composition root for this module's own namespace hook: importing
# finance_schemas is what makes it register its own router (the finance
# summary endpoint) and nest every namespace router registered so far --
# including rules', imported just above -- into `router` itself
# (finance_schemas.py does the nesting; see its own comment for why that has
# to be the ordering-independent side of this import cycle), so
# `app.include_router(router)` (main.py, and every test fixture that only
# imports this module's `router`) mounts finance's endpoints too without
# naming finance anywhere in this module.
#
# Placed at the bottom, not the top: finance_schemas.py imports
# require_api_key and list_beads from this module to build its own route --
# both already defined above by the time this import runs, which is what
# lets finance_schemas.py import `.routes` back without a circular-import
# failure.
from . import finance_schemas  # noqa: E402,F401
