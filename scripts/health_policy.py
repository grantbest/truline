"""The release-health policy model and its loader.

``docs/releases/policy/health-policy.json`` is the one file both the
release-health computation (R26.12 B12, ``apps/factory-dispatcher/release_health.py``)
and its CI gate (B12b, ``release-load.py --check``) read. This module is the
single place that validates it, so the two readers can never disagree about
what a valid policy file looks like.

Three of these thirteen keys (``urgency_bands``, ``aging_days``,
``max_aging_steps``) are also read directly by the ranker,
``apps/factory-dispatcher/queue_order.py``'s ``RankPolicy.from_mapping``. The
ranges declared here for those three keys are written to match that
function's checks exactly -- see ``scripts/tests/test_health_policy.py`` and
``apps/factory-dispatcher/tests/test_health_policy_by_path.py`` for the
parity proof. A mismatch there would let ``release-load.py --check`` pass a
policy file the ranker then refuses on its next tick.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, model_validator

REPO = pathlib.Path(__file__).resolve().parents[1]
HEALTH_POLICY_PATH = REPO / "docs" / "releases" / "policy" / "health-policy.json"


class UrgencyBands(BaseModel):
    """``RankPolicy.from_mapping``'s own rule: ``0 < medium < high <= 1``."""

    model_config = ConfigDict(extra="forbid")

    medium: float
    high: float

    @model_validator(mode="after")
    def _ordered(self) -> "UrgencyBands":
        if not (0 < self.medium < self.high <= 1):
            raise ValueError(
                "urgency_bands must satisfy 0 < medium < high <= 1, "
                f"got medium={self.medium}, high={self.high}"
            )
        return self


class HealthPolicy(BaseModel):
    """The thirteen keys ``health-policy.json`` must hold, exactly.

    ``aging_days``, ``criterion_stale_days``, ``min_classified`` and
    ``max_aging_steps`` are ``StrictInt`` -- a bool, a float (``14.0``) or a
    numeral string (``'14'``) is refused, matching ``RankPolicy.from_mapping``'s
    refusal of the same for ``aging_days`` and ``max_aging_steps``.
    """

    model_config = ConfigDict(extra="forbid")

    urgency_bands: UrgencyBands
    aging_days: StrictInt = Field(gt=0)
    max_aging_steps: StrictInt = Field(ge=0, le=3)
    balance_tolerance_pct: float = Field(ge=0, le=100)
    absent_after_elapsed_pct: float = Field(gt=0, le=1)
    no_landed_work_after_elapsed_pct: float = Field(gt=0, le=1)
    criterion_stale_days: StrictInt = Field(gt=0)
    min_classified: StrictInt = Field(ge=1)
    rework_rate_pct: float = Field(ge=0, le=100)
    unmeasured_actionable_after_hours: float = Field(gt=0)
    re_alert_interval_hours: float = Field(gt=0)
    reconciler_interval_seconds: float = Field(gt=0)
    dispatch_interval_seconds: float = Field(gt=0)


def _git_blob_sha(data: bytes) -> str:
    """The git blob sha of ``data``, computed in process (no ``git`` call) --
    what ``git hash-object`` would print for this content. Mirrors
    ``apps/factory-dispatcher/operator_verbs.py``'s ``_git_blob_sha``."""
    header = f"blob {len(data)}\0".encode()
    return hashlib.sha1(header + data).hexdigest()


def load_health_policy(path: pathlib.Path) -> tuple[HealthPolicy, str]:
    """Read, hash and validate ``path``.

    Raises ``FileNotFoundError`` for a missing file, ``ValueError`` (a
    ``json.JSONDecodeError``) for bytes that are not valid JSON, and
    pydantic's ``ValidationError`` for an invalid policy -- all three
    unwrapped, so a caller can tell the three failure modes apart.
    """
    data = path.read_bytes()
    revision = _git_blob_sha(data)
    content = json.loads(data)
    policy = HealthPolicy.model_validate(content)
    return policy, revision


def _validation_message(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        loc = ".".join(str(part) for part in error["loc"]) or "(content)"
        parts.append(f"{loc}: {error['msg']}")
    return "; ".join(parts)


def health_policy_validation_error(
    content: dict, name: str = "health-policy.json"
) -> Optional[str]:
    """``None`` if ``content`` would pass ``HealthPolicy``, else why not,
    rendered as ``'<name>: <loc>: <msg>'`` for the first error -- the same
    shape ``scripts/release-load.py``'s ``release_content_validation_error``
    uses. ``name`` is the path the caller actually read, so a message about
    a tmp copy never names the committed file.
    """
    try:
        HealthPolicy.model_validate(content)
    except ValidationError as exc:
        return f"{name}: {_validation_message(exc)}"
    return None
