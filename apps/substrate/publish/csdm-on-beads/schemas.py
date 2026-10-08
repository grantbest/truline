"""The CSDM content models — the ``arch.*`` bead types, standing alone.

Extracted from this platform's substrate (``apps/substrate/src/schemas.py``),
which also carries the ``dev.*`` SDLC types and a household finance
extension. Neither belongs here: this module is the service-model half of
the platform, the twelve types the R26.05/O-4 outcome names — "The service
model is installable by someone who is not us" — plus ``arch.risk``, added
R26.09/O-5. It depends on nothing but
``pydantic`` and the standard library, and it registers itself against
``NAMESPACE_TYPE_SCHEMAS`` the same way the substrate's own ``arch``
namespace does, so a caller that only imports this module gets exactly the
CSDM types and nothing else.

See ``bead_rules.py`` alongside this file for the edge vocabulary
(``BEAD_LINK_TYPES``) and the registered state machines — this module needs
neither to define its own content models.
"""

import re
from datetime import date, datetime
from typing import Any, Dict, Literal, Optional, Union
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class NamespaceSchemaRegistry:
    """The content-schema registry a caller consults to validate a bead.

    Populated once, below, by an explicit registration call per type — no
    discovery, no entry-point scan, no dynamic import. A second registration
    for the same (namespace, type) pair overwrites the first, matching a
    module-level dict literal.
    """

    def __init__(self) -> None:
        self._types: Dict[str, Dict[str, type[BaseModel]]] = {}

    def register(self, namespace: str, bead_type: str, model_cls: type[BaseModel]) -> None:
        self._types.setdefault(namespace, {})[bead_type] = model_cls

    def get(self, namespace: str, bead_type: str) -> Optional[type[BaseModel]]:
        return self._types.get(namespace, {}).get(bead_type)

    def types_for(self, namespace: str) -> Dict[str, type[BaseModel]]:
        return dict(self._types.get(namespace, {}))


NAMESPACE_TYPE_SCHEMAS = NamespaceSchemaRegistry()


SourceClass = Literal["authored", "derived", "observed"]

SOURCE_CLASSES: frozenset[str] = frozenset({"authored", "derived", "observed"})

# Historical content written before this field existed carries no
# source_class at all. It reads as authored rather than invalid — the
# conservative default: nothing gets automation-writable just because it
# predates the contract.
DEFAULT_SOURCE_CLASS: SourceClass = "authored"


_REQUIREMENT_REF = re.compile(r"^[A-Z]{2,}-[A-Z]{2,}-\d{3}(/AC-\d+)?$")
_RELEASE_REF = re.compile(r"^R\d{2}\.\d{2}$")
_RELEASE_OUTCOME_ID = re.compile(r"^O-\d+$")
_ARCH_REQUIREMENT_ID = re.compile(r"^[A-Z]{2,}(?:-[A-Z]{2,}){1,3}-\d{3}$")
_REQUIREMENT_MEASUREMENT_KEYS = frozenset(
    {"conformance", "verdict", "measured_at", "measured_revision"}
)
_PRINCIPLE_MEASUREMENT_KEYS = frozenset(
    {"measured_at", "verdict", "applied_count", "conformance"}
)


def _forbidden_measurement_paths(
    value: Any, keys: frozenset[str], path: tuple[str, ...] = ()
) -> list[tuple[str, ...]]:
    """Find dated measurement fields embedded in structure that must not carry them.

    Shared by requirement and principle content: both keep dated
    conformance/verdict measurements in their own observation types, never
    in the structure they measure.
    """
    if isinstance(value, dict):
        paths = []
        for key, child in value.items():
            child_path = (*path, str(key))
            if key in keys:
                paths.append(child_path)
            paths.extend(_forbidden_measurement_paths(child, keys, child_path))
        return paths
    if isinstance(value, list):
        paths = []
        for index, child in enumerate(value):
            paths.extend(_forbidden_measurement_paths(child, keys, (*path, str(index))))
        return paths
    return []


class ArchSourceClassContent(BaseModel):
    """The precedence declaration every ``arch.*`` bead content carries.

    Validated ahead of (and in addition to) any per-type model, so the enum
    is enforced namespace-wide — including for an ``arch`` type with no
    dedicated content model yet.
    """

    model_config = ConfigDict(extra="allow")

    source_class: SourceClass = DEFAULT_SOURCE_CLASS


def read_source_class(content: Dict[str, Any]) -> str:
    """The effective source_class of stored content, defaulting historical data."""
    value = (content or {}).get("source_class")
    return value if value in SOURCE_CLASSES else DEFAULT_SOURCE_CLASS


class ArchContentBase(BaseModel):
    """Common content contract for CSDM ``arch`` beads that model a configuration item.

    Type-specific models below stay additive (``extra="allow"``) so authored
    fields like notes/debt survive while the required architectural evidence
    is enforced.
    """

    model_config = ConfigDict(extra="allow")

    ref: str
    name: str
    description: str
    layer: Literal["demand", "supply"]
    owner: str
    evidence: list[str] = Field(..., min_length=1)
    assessed_at: date
    source_class: SourceClass = DEFAULT_SOURCE_CLASS

    @field_validator("ref", "name", "description", "owner")
    @classmethod
    def arch_required_strings_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("field must not be empty")
        return value

    @field_validator("evidence")
    @classmethod
    def arch_evidence_entries_must_not_be_blank(cls, value: list[str]) -> list[str]:
        if any(not entry.strip() for entry in value):
            raise ValueError("evidence entries must not be empty")
        return value


class ArchCapabilityContent(ArchContentBase):
    """Validated shape for ``arch.capability`` bead content."""

    maturity: Literal["absent", "emerging", "operating", "optimised"]
    supports: list[str] = Field(default_factory=list)


class ArchWorkloadObject(BaseModel):
    """One running object an ``arch.application`` claims to be.

    ``manifest`` is optional and ``managed_by`` may be ``none``, because a
    GitOps orphan is a real state that must be expressible.
    """

    model_config = ConfigDict(extra="allow")

    cluster: str
    namespace: str
    kind: str
    name: str
    manifest: Optional[str] = None
    managed_by: Literal["argocd", "deploy-workflow", "helm", "bootstrap", "none"]


class ArchWorkloadBinding(BaseModel):
    """A structured, locally checkable pointer to an external runtime."""

    model_config = ConfigDict(extra="allow")

    host: str
    compose_path: str
    service: str

    @field_validator("host", "compose_path", "service")
    @classmethod
    def binding_fields_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("field must not be empty")
        return value


class ArchWorkload(BaseModel):
    """What an application actually runs as, or an explicit claim that it does not.

    Required on every ``arch.application``. Keeps *unassessed* and *runs
    nowhere* apart: an object with no workload has not been looked at, while
    ``runtime: none`` is a dated finding.
    """

    model_config = ConfigDict(extra="allow")

    runtime: Literal["kubernetes", "external", "none"]
    objects: list[ArchWorkloadObject] = Field(default_factory=list)
    note: Optional[str] = None
    binding: Optional[ArchWorkloadBinding] = None

    @model_validator(mode="after")
    def workload_shape_matches_runtime(self) -> "ArchWorkload":
        if self.runtime == "kubernetes":
            if not self.objects:
                raise ValueError("runtime 'kubernetes' requires at least one object")
        else:
            if self.objects:
                raise ValueError(f"runtime '{self.runtime}' must not declare objects")
            if self.runtime == "none" and self.binding is not None:
                raise ValueError("runtime 'none' must not declare a binding")
            if self.binding is None and not (self.note or "").strip():
                raise ValueError(
                    f"runtime '{self.runtime}' requires a note saying where it runs, "
                    "or a structured binding"
                )
        return self


class ArchApplicationContent(ArchContentBase):
    """Validated shape for ``arch.application`` bead content."""

    workload: ArchWorkload
    build: Literal["custom", "oss", "saas"]
    business_criticality: Optional[Literal["critical", "high", "medium", "low"]] = None
    technical_health: Literal["healthy", "degraded", "at_risk"]
    business_value: Literal["high", "medium", "low"]
    time_disposition: Literal["invest", "tolerate", "migrate", "eliminate"]
    realizes: list[str] = Field(default_factory=list)
    consumes: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    produces: list[str] = Field(default_factory=list)
    reads: list[str] = Field(default_factory=list)


class ArchServiceContent(ArchContentBase):
    """Validated shape for ``arch.service`` bead content."""

    depends_on: list[str] = Field(default_factory=list)


class ArchInformationObjectContent(ArchContentBase):
    """Validated shape for ``arch.information_object`` bead content."""


class ArchRequirementAcceptanceCriterion(BaseModel):
    """One Gherkin-style criterion inside an ``arch.requirement`` bead."""

    model_config = ConfigDict(extra="allow")

    id: str
    given: str
    when: str
    then: str
    verification: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def criteria_do_not_embed_measurements(cls, value: Any) -> Any:
        paths = _forbidden_measurement_paths(value, _REQUIREMENT_MEASUREMENT_KEYS)
        if paths:
            rendered = ", ".join(".".join(path) for path in paths)
            raise ValueError(
                "acceptance_criteria entries must not embed measurement fields: "
                f"{rendered}"
            )
        return value

    @field_validator("id", "given", "when", "then")
    @classmethod
    def criterion_required_strings_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("field must not be empty")
        return value

    @field_validator("verification")
    @classmethod
    def optional_criterion_strings_must_not_be_blank(
        cls, value: Optional[str]
    ) -> Optional[str]:
        if value is not None and not value.strip():
            raise ValueError("field must not be empty")
        return value


class ArchRequirementUserStoryRationale(BaseModel):
    """The Product Owner's own words, as a declared second shape for ``rationale``.

    Closed (``extra="forbid"``), unlike most content models here that stay
    additive: this is the *second of exactly two* admitted shapes for
    ``rationale`` — a near-miss shape must be refused, not silently parsed.
    """

    model_config = ConfigDict(extra="forbid")

    as_a: str
    i_want: str
    so_that: str

    @field_validator("as_a", "i_want", "so_that")
    @classmethod
    def user_story_strings_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("field must not be empty")
        return value


class ArchRequirementContent(BaseModel):
    """Validated structure for ``arch.requirement`` beads.

    Deliberately omits conformance/verdict fields — those are dated
    measurements and belong in ``arch.requirement_conformance`` beads, not
    requirement structure. ``rationale`` is a declared union of exactly two
    shapes: prose, or a user story (:class:`ArchRequirementUserStoryRationale`).
    """

    model_config = ConfigDict(extra="allow")

    registry_id: str
    id: str
    capability: str
    title: str
    status: Literal["implemented", "partial", "defective", "absent", "rejected"]
    priority: str
    source: str
    rationale: Union[str, ArchRequirementUserStoryRationale]
    acceptance_criteria: list[ArchRequirementAcceptanceCriterion] = Field(
        ..., min_length=1
    )
    source_class: SourceClass = DEFAULT_SOURCE_CLASS

    @model_validator(mode="before")
    @classmethod
    def requirement_top_level_does_not_embed_measurements(cls, value: Any) -> Any:
        if isinstance(value, dict):
            forbidden = sorted(_REQUIREMENT_MEASUREMENT_KEYS.intersection(value))
            if forbidden:
                raise ValueError(
                    "requirement content must not embed measurement fields: "
                    f"{', '.join(forbidden)}"
                )
        return value

    @field_validator("registry_id", "id", "capability", "title", "priority", "source")
    @classmethod
    def requirement_required_strings_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("field must not be empty")
        return value

    @field_validator("rationale")
    @classmethod
    def rationale_prose_must_not_be_blank(
        cls, value: Union[str, "ArchRequirementUserStoryRationale"]
    ) -> Union[str, "ArchRequirementUserStoryRationale"]:
        if isinstance(value, str) and not value.strip():
            raise ValueError("field must not be empty")
        return value

    @field_validator("id")
    @classmethod
    def requirement_id_must_match_registry_pattern(cls, value: str) -> str:
        if not _ARCH_REQUIREMENT_ID.match(value):
            raise ValueError(
                "requirement id must use two to four uppercase segments and "
                "a three digit suffix, e.g. PC-SUB-003"
            )
        return value


class ArchRequirementConformanceContent(BaseModel):
    """A dated verdict against one ``arch.requirement`` criterion — ``arch.requirement_conformance``.

    Deliberately NOT an :class:`ArchContentBase` subclass: a mechanically
    mirrored record has no ``owner``/``layer``/``evidence`` in the authored
    sense those fields exist for. ``source_class`` is locked to
    ``Literal["derived"]`` because this is mirrored, never authored or
    observed.
    """

    model_config = ConfigDict(extra="forbid")

    ref: str
    registry_id: str
    requirement_id: str
    acceptance_criterion_id: Optional[str] = None
    measured_at: date
    verdict: str
    measured_revision: Optional[str] = None
    verification: Optional[str] = None
    source_class: Literal["derived"]

    @field_validator("ref", "registry_id", "requirement_id", "verdict")
    @classmethod
    def conformance_required_strings_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("field must not be empty")
        return value

    @field_validator("acceptance_criterion_id", "measured_revision", "verification")
    @classmethod
    def optional_conformance_strings_must_not_be_blank(
        cls, value: Optional[str]
    ) -> Optional[str]:
        if value is not None and not value.strip():
            raise ValueError("field must not be empty")
        return value

    @field_validator("requirement_id")
    @classmethod
    def conformance_requirement_id_must_match_registry_pattern(cls, value: str) -> str:
        if not _ARCH_REQUIREMENT_ID.match(value):
            raise ValueError(
                "requirement id must use two to four uppercase segments and "
                "a three digit suffix, e.g. PC-SUB-003"
            )
        return value


class ArchPrincipleStatusHistoryEntry(BaseModel):
    """One dated transition inside an ``arch.principle``'s ``status_history``.

    Every field is required: promotions earn their way and demotions explain
    themselves, so a transition without a reason is invalid by design.
    """

    model_config = ConfigDict(extra="allow")

    date: date
    status: Literal["proposed", "adopted", "enforced", "retired"]
    reason: str

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("field must not be empty")
        return value


class ArchPrincipleContent(BaseModel):
    """Validated shape for ``arch.principle`` bead content.

    Deliberately omits staleness/application measurement fields: whether an
    adopted principle is actually applied is a dated ``arch.observation``,
    never principle structure.
    """

    model_config = ConfigDict(extra="allow")

    statement: str
    rationale: str
    source: str
    status: Literal["proposed", "adopted", "enforced", "retired"]
    status_history: list[ArchPrincipleStatusHistoryEntry] = Field(default_factory=list)
    source_class: SourceClass = DEFAULT_SOURCE_CLASS

    @model_validator(mode="before")
    @classmethod
    def principle_does_not_embed_measurements(cls, value: Any) -> Any:
        paths = _forbidden_measurement_paths(value, _PRINCIPLE_MEASUREMENT_KEYS)
        if paths:
            rendered = ", ".join(".".join(path) for path in paths)
            raise ValueError(
                "principle content must not embed measurement fields: "
                f"{rendered}"
            )
        return value

    @field_validator("statement", "rationale", "source")
    @classmethod
    def principle_required_strings_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("field must not be empty")
        return value


class ArchObservedWorkload(BaseModel):
    """Workload identity captured by an ``arch.observation`` bead."""

    model_config = ConfigDict(extra="forbid")

    cluster: str
    namespace: str
    kind: str
    name: str

    @field_validator("cluster", "namespace", "kind", "name")
    @classmethod
    def workload_identity_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("field must not be empty")
        return value


class ArchObservationContent(BaseModel):
    """Validated shape for read-only factual ``arch.observation`` beads.

    Deliberately closed: a cluster reflector may record what it saw, but
    judgment fields like TIME disposition and lifecycle state stay on
    reviewed architecture changes, not reflector writes.
    """

    model_config = ConfigDict(extra="forbid")

    ref: Optional[str] = None
    observed_at: datetime
    workload: ArchObservedWorkload
    image_ref: Optional[str] = None
    replicas: Optional[int] = Field(default=None, ge=0)
    ready_replicas: Optional[int] = Field(default=None, ge=0)
    argocd_sync_status: Optional[str] = None
    argocd_health_status: Optional[str] = None
    last_synced_revision: Optional[str] = None
    source_class: SourceClass = DEFAULT_SOURCE_CLASS

    @field_validator(
        "ref",
        "image_ref",
        "argocd_sync_status",
        "argocd_health_status",
        "last_synced_revision",
    )
    @classmethod
    def optional_observation_strings_must_not_be_blank(
        cls, value: Optional[str]
    ) -> Optional[str]:
        if value is not None and not value.strip():
            raise ValueError("field must not be empty")
        return value

    @model_validator(mode="after")
    def ready_replicas_cannot_exceed_replicas(self) -> "ArchObservationContent":
        if (
            self.replicas is not None
            and self.ready_replicas is not None
            and self.ready_replicas > self.replicas
        ):
            raise ValueError("ready_replicas must be less than or equal to replicas")
        return self


class ArchCiContent(BaseModel):
    """Validated shape for ``arch.ci`` bead content — a technology-layer CI.

    Closed shape, and deliberately NOT an :class:`ArchContentBase` subclass:
    an automated-only existence record has no ``owner``/``evidence``/
    ``assessed_at`` in the authored sense those fields exist for.
    ``source_class`` is locked to ``Literal["observed"]``.
    """

    model_config = ConfigDict(extra="forbid")

    ref: str
    ci_kind: Literal["workload", "namespace", "database"]
    cluster: str
    namespace: str
    kind: str
    name: str
    source_class: Literal["observed"]

    @field_validator("ref", "cluster", "namespace", "kind", "name")
    @classmethod
    def ci_required_strings_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("field must not be empty")
        return value


WORK_CLASSES = ("feature", "enabling", "blocking", "risk", "security")

#: Measurement keys a release charter may never carry — computed at read
#: time, never stored, for the same reason ``ArchRequirementContent`` keeps
#: dated verdicts out of requirement structure.
_RELEASE_MEASUREMENT_KEYS = frozenset(
    {"conformance", "verdict", "measured_at", "measured_revision", "actual_balance"}
)


class ReleaseOutcome(BaseModel):
    """One thing that is tangibly different once a release lands."""

    model_config = ConfigDict(extra="allow")

    id: str
    statement: str
    work_class: Literal["feature", "enabling", "blocking", "risk", "security"]
    requirement_refs: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def outcome_id_must_look_like_an_outcome(cls, value: str) -> str:
        if not _RELEASE_OUTCOME_ID.match(value):
            raise ValueError("outcome id must look like O-1")
        return value

    @field_validator("statement")
    @classmethod
    def outcome_statement_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("outcome statement must not be empty")
        return value

    @field_validator("requirement_refs")
    @classmethod
    def outcome_requirement_refs_must_be_well_formed(cls, value: list[str]) -> list[str]:
        for ref in value:
            if not _REQUIREMENT_REF.match(ref):
                raise ValueError(
                    f"requirement ref {ref!r} must look like LO-CAT-004 or LO-CAT-004/AC-1"
                )
        return value


class ArchReleaseContent(BaseModel):
    """A release — the objective a body of work is aimed at, ``arch.release``.

    Deliberately NOT an :class:`ArchContentBase` subclass: a release is an
    envelope of changes against CIs, not a CI itself. What it does not hold
    is what happened — actual balance is computed from edges at read time,
    never stored here.
    """

    model_config = ConfigDict(extra="allow")

    ref: str
    name: str
    objective: str
    sprints: list[str] = Field(default_factory=list)
    outcomes: list[ReleaseOutcome] = Field(..., min_length=1)
    declared_balance: Dict[str, int] = Field(default_factory=dict)
    opened_at: date
    target_at: Optional[date] = None
    source_class: SourceClass = DEFAULT_SOURCE_CLASS

    @model_validator(mode="before")
    @classmethod
    def release_does_not_embed_measurements(cls, value: Any) -> Any:
        if isinstance(value, dict):
            forbidden = sorted(_RELEASE_MEASUREMENT_KEYS.intersection(value))
            if forbidden:
                raise ValueError(
                    "release content must not embed measurement fields: "
                    f"{', '.join(forbidden)}"
                )
        return value

    @field_validator("ref")
    @classmethod
    def release_ref_must_match_the_id_scheme(cls, value: str) -> str:
        if not _RELEASE_REF.match(value):
            raise ValueError("release ref must look like R26.01")
        return value

    @field_validator("name", "objective")
    @classmethod
    def release_required_strings_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("field must not be empty")
        return value

    @model_validator(mode="after")
    def outcome_ids_are_unique(self) -> "ArchReleaseContent":
        seen: set[str] = set()
        for outcome in self.outcomes:
            if outcome.id in seen:
                raise ValueError(f"duplicate outcome id {outcome.id!r}")
            seen.add(outcome.id)
        return self

    @model_validator(mode="after")
    def declared_balance_is_a_legal_mix(self) -> "ArchReleaseContent":
        if not self.declared_balance:
            return self
        illegal = sorted(set(self.declared_balance) - set(WORK_CLASSES))
        if illegal:
            raise ValueError(
                "declared_balance names unknown work class(es): "
                f"{', '.join(illegal)}; legal values are {', '.join(WORK_CLASSES)}"
            )
        total = sum(self.declared_balance.values())
        if total != 100:
            raise ValueError(f"declared_balance must sum to 100, got {total}")
        return self

    @model_validator(mode="after")
    def declared_classes_are_reachable_through_an_outcome(self) -> "ArchReleaseContent":
        declared = {
            work_class
            for work_class, share in self.declared_balance.items()
            if share > 0
        }
        present = {outcome.work_class for outcome in self.outcomes}
        missing = sorted(declared - present)
        if missing:
            raise ValueError(
                "declared_balance gives a non-zero share to work class(es) with "
                f"no outcome to deliver them: {', '.join(missing)}"
            )
        return self


class ArchChangeContent(BaseModel):
    """An ITIL change record — ``arch.change``.

    Deliberately NOT an :class:`ArchContentBase` subclass: a change is an
    *event against* a CI, carried by an ``affects`` edge, not a CI itself.
    """

    model_config = ConfigDict(extra="allow")

    change_type: Literal["standard", "normal", "emergency"]
    summary: str
    applications: list[str] = Field(..., min_length=1)
    capabilities: list[str] = Field(default_factory=list)
    release_ref: Optional[str] = None
    implemented_at: Optional[datetime] = None
    evidence: list[str] = Field(..., min_length=1)
    source_class: SourceClass = DEFAULT_SOURCE_CLASS

    @field_validator("evidence")
    @classmethod
    def change_evidence_entries_must_not_be_blank(cls, value: list[str]) -> list[str]:
        if any(not entry.strip() for entry in value):
            raise ValueError("evidence entries must not be empty")
        return value


_INCIDENT_RUNBOOK_PREFIX = "docs/runbooks/"


class ArchIncidentContent(BaseModel):
    """A production incident — ``arch.incident``.

    Deliberately NOT an :class:`ArchContentBase` subclass, for the reason
    :class:`ArchChangeContent` already gives: an incident is an *event
    against* a CI (carried by the ``affects`` edge to ``arch.application``),
    not a CI itself. ``severity`` is a closed literal rather than an import
    from an alerting system: this module has no runtime dependency on one.
    ``runbook_refs`` keeps runbooks as documents, reachable by path, rather
    than minting a new edge type for them.
    """

    model_config = ConfigDict(extra="allow")

    summary: str
    severity: Literal["urgent", "actionable", "informational"]
    detected_at: datetime
    resolved_at: Optional[datetime] = None
    source: str
    applications: list[str] = Field(..., min_length=1)
    resolution: Optional[str] = None
    runbook_refs: list[str] = Field(default_factory=list)
    source_class: SourceClass = DEFAULT_SOURCE_CLASS

    @field_validator("summary", "source")
    @classmethod
    def incident_required_strings_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("field must not be empty")
        return value

    @field_validator("resolution")
    @classmethod
    def incident_resolution_must_not_be_blank_if_present(
        cls, value: Optional[str]
    ) -> Optional[str]:
        if value is not None and not value.strip():
            raise ValueError("field must not be empty")
        return value

    @field_validator("runbook_refs")
    @classmethod
    def runbook_refs_must_be_non_blank_docs_runbooks_paths(
        cls, value: list[str]
    ) -> list[str]:
        for entry in value:
            if not entry.strip():
                raise ValueError("runbook_refs entries must not be empty")
            if not entry.startswith(_INCIDENT_RUNBOOK_PREFIX):
                raise ValueError(
                    f"runbook_refs entries must be paths under {_INCIDENT_RUNBOOK_PREFIX}"
                    f", got {entry!r}"
                )
        return value

    @model_validator(mode="after")
    def resolved_at_cannot_precede_detected_at(self) -> "ArchIncidentContent":
        if self.resolved_at is not None and self.resolved_at < self.detected_at:
            raise ValueError("resolved_at must not precede detected_at")
        return self


class ArchRiskContent(BaseModel):
    """An accepted risk, its owner, and its review date — ``arch.risk``.

    Deliberately NOT an :class:`ArchContentBase` subclass, for the reason
    :class:`ArchPrincipleContent` already gives: a risk is a standing
    governance record with its own status ladder, not a configuration item.
    ``severity`` reuses :class:`ArchApplicationContent`'s
    ``business_criticality`` vocabulary rather than :class:`ArchIncidentContent`'s
    alerting-severity one — the two measure different things.
    ``decision_ref`` is a free-text citation to the amendment or decision
    record that accepted the risk, not a bead ref: not every acceptance
    decision has a bead of its own.
    """

    model_config = ConfigDict(extra="allow")

    statement: str
    owner: str
    accepted_at: date
    review_by: date
    severity: Literal["critical", "high", "medium", "low"]
    decision_ref: str
    source_class: SourceClass = DEFAULT_SOURCE_CLASS

    @field_validator("statement", "owner", "decision_ref")
    @classmethod
    def risk_required_strings_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("field must not be empty")
        return value

    @model_validator(mode="after")
    def review_by_cannot_precede_accepted_at(self) -> "ArchRiskContent":
        if self.review_by < self.accepted_at:
            raise ValueError("review_by must not precede accepted_at")
        return self


_RELEASE_HEALTH_SIGNAL_IDS = (
    "class_absent",
    "class_over_share",
    "outcome_no_landed_work",
    "outcome_unmeasured",
    "criterion_stale",
    "nothing_delivering",
    "unbound_closed_in_window",
    "queue_starvation",
    "time_unmeasurable",
    "rework",
    "merged_without_verdict",
)

_RELEASE_HEALTH_REF_PREFIX = "health."


class ArchReleaseHealthTimeBox(BaseModel):
    """A release's own date window, read from its charter. Both dates are
    ``None`` together when the charter has no (parseable) target date.
    """

    model_config = ConfigDict(extra="forbid")

    opened_at: Optional[str] = None
    target_at: Optional[str] = None
    elapsed_pct: Optional[float] = None


class ArchReleaseHealthSignal(BaseModel):
    """One fired/pending/clear reading over the closed eleven-id signal set.

    ``fired`` is ``"unknown"`` rather than ``False`` when the input the
    signal depends on could not be read.
    """

    model_config = ConfigDict(extra="forbid")

    id: Literal[_RELEASE_HEALTH_SIGNAL_IDS]  # type: ignore[valid-type]
    fired: Literal[True, False, "unknown"]
    pending: bool
    impact: Literal["low", "medium", "high"]
    evidence: str
    since: Optional[str] = None


class ArchReleaseHealthBalanceEntry(BaseModel):
    """One work class's declared share against what the bound work shows.

    ``actual_pct`` is ``None`` exactly when ``insufficient_sample`` is
    ``True`` — never a computed zero standing in for "not enough data".
    """

    model_config = ConfigDict(extra="forbid")

    work_class: Literal["feature", "enabling", "blocking", "risk", "security"]
    declared_pct: int
    actual_count: int
    actual_pct: Optional[float] = None
    absent: bool
    insufficient_sample: bool


class ArchReleaseHealthOutcome(BaseModel):
    """One release outcome's delivery state, derived from its bound tasks."""

    model_config = ConfigDict(extra="forbid")

    id: str
    work_class: Literal["feature", "enabling", "blocking", "risk", "security"]
    delivery: Literal["delivered", "in_progress", "declared_only", "unmeasured", "retired"]
    tasks_by_state: Dict[str, int] = Field(default_factory=dict)
    unmeasured_candidates: list[str] = Field(default_factory=list)


class ArchReleaseHealthCriterion(BaseModel):
    """One cited acceptance criterion's staleness/change read, by ``ref``."""

    model_config = ConfigDict(extra="forbid")

    ref: str
    stale: bool
    changed: bool
    unmeasurable: bool


class ArchReleaseHealthChanges(BaseModel):
    """Counts over the changes bound to a release.

    Each ``int | None`` field is ``None`` exactly when ``"changes"`` is
    listed in ``coverage.unreadable`` (``coverage.changes_read`` is
    ``False``) — never a computed zero standing in for "none found".
    """

    model_config = ConfigDict(extra="forbid")

    count: Optional[int] = None
    by_change_type: Dict[str, int] = Field(default_factory=dict)
    without_verdict_record: Optional[int] = None


class ArchReleaseHealthRework(BaseModel):
    """Task-level rework counts: a bound task counts once regardless of how
    many request-changes notes or requeues it collected.
    """

    model_config = ConfigDict(extra="forbid")

    bound: Optional[int] = None
    request_changes_tasks: Optional[int] = None
    requeued_tasks: Optional[int] = None
    union: Optional[int] = None
    superseded: Optional[int] = None
    rate: Optional[float] = None
    regresses_edges_read: int
    coverage_note: str


class ArchReleaseHealthCloseReadiness(BaseModel):
    """Whether a release's outcomes and criteria look closeable right now.

    ``ready`` is ``"unknown"`` rather than ``False`` when a required input
    could not be read.
    """

    model_config = ConfigDict(extra="forbid")

    ready: Literal[True, False, "unknown"]
    blockers: list[str] = Field(default_factory=list)


class ArchReleaseHealthQueue(BaseModel):
    """A snapshot of the queue-order file's claimable-age reading, by class.

    ``oldest_claimable_age_days_by_class`` is the literal string
    ``"unknown"`` (not an empty dict) when ``"queue_order_file"`` is listed
    in ``coverage.unreadable`` (``coverage.queue_order_file_read`` is
    ``False``) -- the file was missing, stale, or unreadable.
    """

    model_config = ConfigDict(extra="forbid")

    oldest_claimable_age_days_by_class: Union[Dict[str, float], Literal["unknown"]]
    read_from: str
    computed_at: Optional[str] = None


class ArchReleaseHealthCoverage(BaseModel):
    """Which inputs this measurement actually read — the honesty ledger
    every other ``None``/``"unknown"`` field elsewhere can be traced to.
    """

    model_config = ConfigDict(extra="forbid")

    complete: bool
    unreadable: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    tasks_read: bool
    delivers_read: bool
    conformances_read: bool
    time_box_read: bool
    policy_read: bool
    changes_read: bool
    queue_order_file_read: bool


class ArchReleaseHealthDisposition(BaseModel):
    """The operator note that moved a drifting/breached record to ``accepted``.

    ``fired_ids`` is the sorted fired signal ids *at the measurement that
    entered* ``accepted`` — a later measurement's accepted rule compares its
    own fired set against this snapshot.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["accepted", "replanned", "rechartered"]
    until: str
    note_id: str
    recorded_by: str
    fired_ids: list[str] = Field(default_factory=list)


class ArchReleaseHealthContent(BaseModel):
    """A release's governed health posture — ``arch.release_health``.

    Deliberately closed (``extra="forbid"``), unlike
    :class:`ArchIncidentContent`/:class:`ArchRiskContent`'s
    ``extra="allow"``: those are envelopes over authored prose or governance
    records meant to grow new fields under review, while this type is a
    reconciler's derivation with no authored-prose use case at all. The
    precedent is :class:`ArchObservationContent`, the other automated-only
    closed arch model.

    This module carries the shape and the state machine (alongside this
    file); deriving and writing the content is the owning platform's job.
    """

    model_config = ConfigDict(extra="forbid")

    ref: str
    release_ref: str
    measured_at: str
    measured_revision: str
    policy_revision: str
    time_box: ArchReleaseHealthTimeBox
    urgency: Literal["low", "medium", "high", "unknown"]
    signals: list[ArchReleaseHealthSignal] = Field(default_factory=list)
    balance: list[ArchReleaseHealthBalanceEntry] = Field(default_factory=list)
    outcomes: list[ArchReleaseHealthOutcome] = Field(default_factory=list)
    criteria: list[ArchReleaseHealthCriterion] = Field(default_factory=list)
    changes: ArchReleaseHealthChanges
    rework: ArchReleaseHealthRework
    close_readiness: ArchReleaseHealthCloseReadiness
    queue: ArchReleaseHealthQueue
    coverage: ArchReleaseHealthCoverage
    disposition: Optional[ArchReleaseHealthDisposition] = None
    source_class: Literal["derived"]

    @field_validator("ref")
    @classmethod
    def ref_must_be_a_health_ref(cls, value: str) -> str:
        if not value.startswith(_RELEASE_HEALTH_REF_PREFIX):
            raise ValueError(
                f"ref must start with {_RELEASE_HEALTH_REF_PREFIX!r}, got {value!r}"
            )
        return value

    @field_validator("signals")
    @classmethod
    def signals_have_no_duplicate_id(
        cls, value: list[ArchReleaseHealthSignal]
    ) -> list[ArchReleaseHealthSignal]:
        seen: set[str] = set()
        for signal in value:
            if signal.id in seen:
                raise ValueError(f"duplicate signal id {signal.id!r}")
            seen.add(signal.id)
        return value

    @field_validator("balance")
    @classmethod
    def balance_actual_pct_is_null_when_insufficient(
        cls, value: list[ArchReleaseHealthBalanceEntry]
    ) -> list[ArchReleaseHealthBalanceEntry]:
        for entry in value:
            if entry.insufficient_sample and entry.actual_pct is not None:
                raise ValueError(
                    f"balance entry for {entry.work_class!r} has insufficient_sample=True "
                    "but a non-null actual_pct"
                )
        return value

    @model_validator(mode="after")
    def coverage_complete_implies_nothing_unreadable(self) -> "ArchReleaseHealthContent":
        if self.coverage.complete and self.coverage.unreadable:
            raise ValueError(
                "coverage.complete is True but coverage.unreadable is non-empty: "
                f"{self.coverage.unreadable}"
            )
        return self


#: Map of (arch) bead type -> Pydantic model — the fourteen CSDM content
#: models this tree publishes. Unknown arch types keep the documented
#: pass-through behaviour of :func:`validate_bead_content`.
ARCH_TYPE_SCHEMAS: Dict[str, type[BaseModel]] = {
    "capability": ArchCapabilityContent,
    "application": ArchApplicationContent,
    "service": ArchServiceContent,
    "information_object": ArchInformationObjectContent,
    "requirement": ArchRequirementContent,
    "requirement_conformance": ArchRequirementConformanceContent,
    "principle": ArchPrincipleContent,
    "observation": ArchObservationContent,
    "change": ArchChangeContent,
    "incident": ArchIncidentContent,
    "ci": ArchCiContent,
    "release": ArchReleaseContent,
    "risk": ArchRiskContent,
    "release_health": ArchReleaseHealthContent,
}

for _arch_bead_type, _arch_model_cls in ARCH_TYPE_SCHEMAS.items():
    NAMESPACE_TYPE_SCHEMAS.register("arch", _arch_bead_type, _arch_model_cls)
del _arch_bead_type, _arch_model_cls


def validate_bead_content(namespace: str, bead_type: str, content: Dict[str, Any]) -> None:
    """Raise ``pydantic.ValidationError`` for modelled namespace/type content.

    Unknown types remain pass-through until their contracts harden — except
    the ``source_class`` reconciliation contract, which every ``arch`` bead
    accepts regardless of whether its type has a dedicated content model yet.
    """
    if namespace == "arch":
        ArchSourceClassContent.model_validate(content)

    model_cls = NAMESPACE_TYPE_SCHEMAS.get(namespace, bead_type)
    if model_cls is None:
        return
    model_cls.model_validate(content)


def validate_arch_content(bead_type: str, content: Dict[str, Any]) -> None:
    """Raise ``pydantic.ValidationError`` if ``content`` doesn't match the model for ``bead_type``."""
    validate_bead_content("arch", bead_type, content)


__all__ = [
    "ArchApplicationContent",
    "ArchCapabilityContent",
    "ArchChangeContent",
    "ArchCiContent",
    "ArchContentBase",
    "ArchIncidentContent",
    "ArchInformationObjectContent",
    "ArchObservationContent",
    "ArchObservedWorkload",
    "ArchPrincipleContent",
    "ArchPrincipleStatusHistoryEntry",
    "ArchReleaseContent",
    "ArchRequirementAcceptanceCriterion",
    "ArchRequirementConformanceContent",
    "ArchRequirementContent",
    "ArchRequirementUserStoryRationale",
    "ArchServiceContent",
    "ArchSourceClassContent",
    "ArchWorkload",
    "ArchWorkloadBinding",
    "ArchWorkloadObject",
    "ARCH_TYPE_SCHEMAS",
    "DEFAULT_SOURCE_CLASS",
    "NAMESPACE_TYPE_SCHEMAS",
    "NamespaceSchemaRegistry",
    "ReleaseOutcome",
    "SourceClass",
    "SOURCE_CLASSES",
    "WORK_CLASSES",
    "read_source_class",
    "validate_arch_content",
    "validate_bead_content",
]
