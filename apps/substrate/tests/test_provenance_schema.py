from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.schemas import BeadCreate, BeadRead, BeadProvenance


def complete_provenance() -> dict:
    return {
        "worker": "codex",
        "model": "gpt-5",
        "prompt_ref": "dev.task/123",
        "tokens": 1280,
        "cost_usd": 0.42,
        "duration_s": 12.5,
    }


def bead_create_payload(**overrides) -> dict:
    payload = {
        "namespace": "dev",
        "type": "note",
        "state": "active",
        "trust_tier": "system",
        "created_by": "factory-dispatcher/codex",
        "content": {"kind": "status", "body": "done"},
    }
    payload.update(overrides)
    return payload


def test_provenance_declares_the_agent_run_shape():
    record = BeadProvenance.model_validate(complete_provenance())

    assert record.model_dump() == complete_provenance()


def test_provenance_still_accepts_every_existing_stored_record_unchanged():
    """PC-INF-003/AC-2: nullable tokens/cost_usd must not break history.

    Every record written before this change carries concrete non-negative
    int/float values for both fields, which is still exactly what
    ``Optional[int]``/``Optional[float]`` accept - no migration required.
    """
    record = BeadProvenance.model_validate(complete_provenance())

    assert record.tokens == 1280
    assert record.cost_usd == 0.42


def test_provenance_accepts_unmeasured_tokens_and_cost_as_null():
    """A worker run that reported no usage figures records None, not 0 - the
    two must stay distinguishable to a reader of the bead."""
    provenance = complete_provenance() | {"tokens": None, "cost_usd": None}

    record = BeadProvenance.model_validate(provenance)

    assert record.tokens is None
    assert record.cost_usd is None
    assert record.model_dump() == provenance


def test_provenance_tokens_and_cost_usd_remain_required_keys_even_when_null():
    """Nullable is not the same as optional-to-omit: the key must still be
    present, so ``extra='forbid'``'s "all six fields required" guarantee
    survives the schema change."""
    provenance = complete_provenance()
    del provenance["tokens"]

    with pytest.raises(ValidationError) as exc_info:
        BeadProvenance.model_validate(provenance)

    assert ("tokens",) in {tuple(error["loc"]) for error in exc_info.value.errors()}


def test_provenance_still_rejects_a_negative_measured_token_count():
    provenance = complete_provenance() | {"tokens": -1}

    with pytest.raises(ValidationError):
        BeadProvenance.model_validate(provenance)


def test_agent_authored_bead_create_requires_complete_provenance():
    with pytest.raises(ValidationError) as exc_info:
        BeadCreate.model_validate(bead_create_payload(provenance={}))

    assert "agent-authored beads require a complete provenance record" in str(exc_info.value)


def test_agent_authored_bead_create_rejects_partial_provenance():
    provenance = complete_provenance()
    del provenance["cost_usd"]

    with pytest.raises(ValidationError) as exc_info:
        BeadCreate.model_validate(bead_create_payload(provenance=provenance))

    assert ("provenance", "cost_usd") in {
        tuple(error["loc"]) for error in exc_info.value.errors()
    }


def test_agent_authored_bead_create_rejects_extra_provenance_fields():
    provenance = complete_provenance() | {"git_sha": "5bada9d"}

    with pytest.raises(ValidationError) as exc_info:
        BeadCreate.model_validate(bead_create_payload(provenance=provenance))

    assert ("provenance", "git_sha") in {
        tuple(error["loc"]) for error in exc_info.value.errors()
    }


def test_agent_authored_bead_create_accepts_complete_provenance():
    bead = BeadCreate.model_validate(
        bead_create_payload(provenance=complete_provenance())
    )

    assert bead.provenance["worker"] == "codex"


def test_user_authored_bead_create_can_omit_provenance_during_backfill():
    bead = BeadCreate.model_validate(
        bead_create_payload(
            trust_tier="user",
            created_by="factory-dispatcher/file-task",
            provenance={},
        )
    )

    assert bead.provenance == {}


def test_bead_read_accepts_legacy_empty_provenance():
    now = datetime.now(timezone.utc)
    bead = BeadRead.model_validate(
        bead_create_payload(
            id=uuid4(),
            created_at=now,
            updated_at=now,
            provenance={},
        )
    )

    assert bead.provenance == {}


def test_bead_read_preserves_legacy_migration_provenance():
    now = datetime.now(timezone.utc)
    provenance = {
        "migrated_from": {
            "bead_id": "4dc53b94-2ece-4569-84df-ff1fbd6534bb",
            "namespace": "platform-substrate",
        }
    }

    bead = BeadRead.model_validate(
        bead_create_payload(
            id=uuid4(),
            created_at=now,
            updated_at=now,
            provenance=provenance,
        )
    )

    assert bead.provenance == provenance
