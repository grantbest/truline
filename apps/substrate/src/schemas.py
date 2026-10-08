import re
from datetime import date, datetime
from typing import Any, Dict, Literal, NamedTuple, Optional, Union
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, UUID4, field_validator, model_validator

try:
    from .bead_rules import BEAD_LINK_TYPES, HUMAN_EXEMPTION, SOURCE_CLASS_WRITERS
except ImportError:  # pragma: no cover - bare-module consumers
    # Three CI-exercised tests (scripts and dispatcher lanes) import this
    # module bare after sys.path-inserting apps/substrate/src; a relative
    # import has no parent package there (release-gate finding on the PR
    # that extracted bead_rules).
    from bead_rules import BEAD_LINK_TYPES, HUMAN_EXEMPTION, SOURCE_CLASS_WRITERS

class NamespaceSchemaRegistry:
    """The content-schema registry the core owns.

    Namespaces this module defines directly (``dev``, ``arch``) register
    themselves against this instance right after their type maps are built,
    below. A namespace this module does not define — ``finance`` is the one
    that exists today — registers itself the same way from its own module
    (``finance_schemas.py``), which this module never imports at module level
    (``validate_finance_content`` imports it lazily, as a safety net). Registration
    is the only way a (namespace, type) pair ever becomes known here: there
    is no discovery, no entry-point scan, and no dynamic import.

    A second registration for the same (namespace, type) pair overwrites the
    first, matching the module-level dict literal this replaces — refusing a
    duplicate would be new behaviour, and this change is structural.
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


class DevTaskVerification(BaseModel):
    """Worker self-check instructions for a ``dev.task`` bead."""

    model_config = ConfigDict(extra="allow")

    commands: list[str]
    must_report_unverified: bool = True
    expect_pristine_failure: bool = False


class DevTaskScope(BaseModel):
    """Filesystem write boundaries for a ``dev.task`` bead."""

    model_config = ConfigDict(extra="allow")

    paths: list[str]
    forbidden_paths: list[str]

    @field_validator("forbidden_paths")
    @classmethod
    def must_forbid_github_workflows(cls, value: list[str]) -> list[str]:
        if ".github/workflows/**" not in value:
            raise ValueError("forbidden_paths must include .github/workflows/**")
        return value


class DevTaskBudget(BaseModel):
    """Budget limits for a ``dev.task`` worker attempt."""

    model_config = ConfigDict(extra="allow")

    max_agent_minutes: int
    max_usd: float
    max_tokens: int


class NFR(BaseModel):
    """A non-functional constraint, in a shape QA can turn into a test.

    Prose was the obvious alternative and would have been cheaper to write. It
    also reproduces the gap it was meant to close: an NFR that is recorded and
    still not asserted. ``threshold`` is the bound a test can compare against,
    and ``verification`` is how you would prove it — without both, this is a
    comment.
    """

    model_config = ConfigDict(extra="allow")

    category: Literal[
        "latency", "availability", "durability", "security",
        "cost", "observability", "usability",
    ]
    statement: str
    threshold: str
    verification: str


class ArchImpact(BaseModel):
    """CSDM objects a change touches, declared by id.

    Declared rather than derived: the portfolio maps applications to Kubernetes
    manifests, not to source paths, so there is no ``scope.paths`` -> application
    mapping to infer from. Building one is separate work with its own value.
    """

    model_config = ConfigDict(extra="allow")

    applications: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    notes: Optional[str] = None


_REQUIREMENT_REF = re.compile(r"^[A-Z]{2,}-[A-Z]{2,}-\d{3}(/AC-\d+)?$")
_RELEASE_REF = re.compile(r"^R\d{2}\.\d{2}$")
_RELEASE_OUTCOME_ID = re.compile(r"^O-\d+$")
#: A task-side reference to a release, optionally naming one of its outcomes.
#: ``R26.01`` binds the task to the release; ``R26.01/O-2`` also names which
#: outcome it delivers. Only the outcome half is ever persisted on the task —
#: see ``DevTaskContent.outcome_ref``.
_RELEASE_TASK_REF = re.compile(r"^R\d{2}\.\d{2}(/O-\d+)?$")
_ARCH_REQUIREMENT_ID = re.compile(r"^[A-Z]{2,}(?:-[A-Z]{2,}){1,3}-\d{3}$")
_REQUIREMENT_MEASUREMENT_KEYS = frozenset(
    {"conformance", "verdict", "measured_at", "measured_revision"}
)


def _forbidden_requirement_measurement_paths(
    value: Any, path: tuple[str, ...] = ()
) -> list[tuple[str, ...]]:
    """Find dated measurement fields embedded in requirement structure."""
    if isinstance(value, dict):
        paths = []
        for key, child in value.items():
            child_path = (*path, str(key))
            if key in _REQUIREMENT_MEASUREMENT_KEYS:
                paths.append(child_path)
            paths.extend(_forbidden_requirement_measurement_paths(child, child_path))
        return paths
    if isinstance(value, list):
        paths = []
        for index, child in enumerate(value):
            paths.extend(
                _forbidden_requirement_measurement_paths(child, (*path, str(index)))
            )
        return paths
    return []


class DevTaskContent(BaseModel):
    """Validated shape for ``dev.task`` bead content."""

    model_config = ConfigDict(extra="allow")

    lane: Literal["code-health", "drift", "bug-triage", "feature"]
    title: str
    intent: str
    context_refs: list[str]
    source_bead_ids: list[str] = Field(default_factory=list)
    acceptance: list[str] = Field(..., min_length=1)
    verification: DevTaskVerification
    scope: DevTaskScope
    risk_class: Literal["structural", "behavioral"]
    budget: DevTaskBudget

    # --- traceability (2026-08-02) -------------------------------------------
    # Optional by decision, not by oversight: the rollout warns for one sprint
    # before it refuses, because the in-flight non-conforming population cannot
    # currently be counted. See docs/plans/2026-08-02-bead-traceability-contract.md.
    requirement_refs: list[str] = Field(default_factory=list)
    nfrs: list[NFR] = Field(default_factory=list)
    arch_impact: Optional[ArchImpact] = None
    pr_refs: list[str] = Field(default_factory=list)

    # --- release traceability (2026-08-25) -----------------------------------
    # WHICH release a task delivers is the ``delivers`` edge and lives nowhere
    # else: two representations of one fact is the failure this repository
    # keeps paying for. What is left here is the part no edge can carry —
    # which outcome *within* that release, and, when no release applies, the
    # written reason. ``outcome_ref`` is deliberately the bare ``O-2`` rather
    # than ``R26.01/O-2``: it is meaningless without the edge, so it cannot
    # drift away from it.
    #
    # Both optional, for the reason requirement_refs is: the refusal belongs at
    # filing (apps/factory-dispatcher/file_task.py), which is the boundary
    # between what we do from now on and what we already did. Enforcing it here
    # would make every historical bead unreadable, and their content records
    # what was true when they ran.
    outcome_ref: Optional[str] = None
    release_ref_waived: Optional[str] = None

    # --- emergency marker (R26.12/B15) ---------------------------------------
    # Intent recorded on the bead, not execution state: nothing here reads these
    # fields or lets them affect claim order, dispatch, or rank. B17 sets them
    # (--expedite); B16 refuses an unattested one at intake; B7's rank key k0
    # honours one only under an operator-note attestation. The three travel
    # together or not at all, enforced below.
    class_of_service: Optional[Literal["emergency"]] = None
    expedite_reason: Optional[str] = None
    expedite_until: Optional[AwareDatetime] = None

    @field_validator("expedite_until", mode="before")
    @classmethod
    def expedite_until_must_be_an_iso8601_string(cls, value: Any) -> Any:
        """Reject before Pydantic's lax mode can coerce an int into a datetime.

        Lax mode treats an int or a numeric string as epoch seconds, which would
        let an operator-set ``expedite_until=1791000000`` through untouched into
        stored content. Requiring an ISO-8601 string here, ahead of
        ``AwareDatetime``'s own coercion, closes that gap while leaving the
        naive-datetime rejection (an ISO string with no timezone) to
        ``AwareDatetime`` as before.
        """
        if value is None:
            return value
        if not isinstance(value, str):
            raise ValueError("expedite_until must be an ISO-8601 datetime string")
        try:
            datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("expedite_until must be an ISO-8601 datetime string") from exc
        return value

    @model_validator(mode="after")
    def emergency_marker_fields_travel_together(self) -> "DevTaskContent":
        if self.class_of_service == "emergency":
            if not (self.expedite_reason or "").strip():
                raise ValueError(
                    "expedite_reason is required when class_of_service is 'emergency'"
                )
            if self.expedite_until is None:
                raise ValueError(
                    "expedite_until is required when class_of_service is 'emergency'"
                )
        else:
            if self.expedite_reason is not None:
                raise ValueError(
                    "expedite_reason is only valid when class_of_service is 'emergency'"
                )
            if self.expedite_until is not None:
                raise ValueError(
                    "expedite_until is only valid when class_of_service is 'emergency'"
                )
        return self

    @field_validator("requirement_refs")
    @classmethod
    def requirement_refs_must_be_well_formed(cls, value: list[str]) -> list[str]:
        """Accept requirement- or criterion-level references.

        A task usually satisfies specific criteria and sometimes a whole
        requirement, so both ``LO-CAT-004`` and ``LO-CAT-004/AC-1`` are valid.
        Free text is not: an unresolvable reference is worse than none, because
        it reads as traceability while pointing nowhere.
        """
        for ref in value:
            if not _REQUIREMENT_REF.match(ref):
                raise ValueError(
                    f"requirement ref {ref!r} must look like LO-CAT-004 or LO-CAT-004/AC-1"
                )
        return value

    @field_validator("outcome_ref")
    @classmethod
    def outcome_ref_must_be_a_bare_outcome_id(cls, value: Optional[str]) -> Optional[str]:
        """``O-2``, never ``R26.01/O-2``.

        The release half belongs to the edge. Accepting it here would create a
        second place the release id is written, and the two could then disagree
        about which release a task is in — with the string being the one the
        author reads and the edge being the one every query uses.
        """
        if value is None:
            return value
        if not _RELEASE_OUTCOME_ID.match(value):
            raise ValueError(
                "outcome_ref must be a bare outcome id such as O-2; the release "
                "itself is carried by the delivers edge, not by content"
            )
        return value

    worker_hint: Optional[Literal["codex", "claude", "gemini", "local"]] = None
    autonomy: Literal["propose", "auto-merge-eligible"] = "propose"


class DevNoteContent(BaseModel):
    """Validated shape for ``dev.note`` collaboration content."""

    model_config = ConfigDict(extra="allow")

    kind: Literal["comment", "question", "answer", "status", "attachment", "review"]
    body: str
    blocking: bool = False
    answers_ref: Optional[str] = None
    url: Optional[str] = None
    verdict: Optional[Literal["approve", "request-changes"]] = None

    @field_validator("body")
    @classmethod
    def body_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("body must not be empty")
        return value

    @model_validator(mode="after")
    def validate_kind_specific_fields(self) -> "DevNoteContent":
        if self.kind == "answer":
            if self.answers_ref is None:
                raise ValueError("answers_ref is required for answer notes")
        elif "answers_ref" in self.model_fields_set:
            raise ValueError("answers_ref is only valid for answer notes")

        if self.kind == "attachment":
            if self.url is None:
                raise ValueError("url is required for attachment notes")
        elif "url" in self.model_fields_set:
            raise ValueError("url is only valid for attachment notes")

        if self.kind == "review":
            if self.verdict is None:
                raise ValueError("verdict is required for review notes")
        elif "verdict" in self.model_fields_set:
            raise ValueError("verdict is only valid for review notes")

        if self.kind != "question" and (
            self.blocking or "blocking" in self.model_fields_set
        ):
            raise ValueError("blocking is only valid for question notes")

        return self


# Map of (dev) bead type -> Pydantic model. POST /beads consults
# this when namespace == "dev"; unknown types pass through.
class DevDesignContent(BaseModel):
    """The architect's decision record for a task — ``dev.design``.

    Linked to its task by a ``designs`` edge rather than embedded, because the
    decision outlives the task: a later change wanting to know why something is
    shaped this way should not have to find the task that happened to carry it.
    """

    model_config = ConfigDict(extra="allow")

    decision: str
    rationale: str
    alternatives_rejected: list[str] = Field(default_factory=list)
    nfrs_derived: list[NFR] = Field(default_factory=list)
    arch_impact: Optional[ArchImpact] = None

    @field_validator("decision", "rationale")
    @classmethod
    def design_strings_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("field must not be empty")
        return value


class TestResult(BaseModel):
    """One suite's outcome inside a gate run.

    ``not-run`` is deliberately distinct from ``skipped``: a suite nobody
    executed is not the same claim as a suite that ran and excluded cases, and
    collapsing them is how a gate reports green for work it never checked.
    """

    model_config = ConfigDict(extra="allow")

    category: Literal[
        "unit", "regression", "negative", "resilience",
        "usability", "security", "performance", "lint",
    ]
    suite: str
    outcome: Literal["pass", "fail", "skipped", "not-run"]
    evidence: str


class DevReleaseContent(BaseModel):
    """A gate run over a set of staged PRs — ``dev.release``.

    This is the release loop's record of what it required, what it ran, and what
    it concluded. It is what makes a verdict re-litigable later against the
    inputs that produced it.
    """

    model_config = ConfigDict(extra="allow")

    reviewer: str
    verdict: Literal["merge", "merge-with-changes", "do-not-merge"]
    pr_refs: list[str] = Field(..., min_length=1)
    task_refs: list[str] = Field(default_factory=list)
    required_suites: list[str] = Field(default_factory=list)
    results: list[TestResult] = Field(default_factory=list)
    merge_order: list[str] = Field(default_factory=list)
    unverified_claims: list[str] = Field(default_factory=list)
    not_reviewed: list[str] = Field(default_factory=list)

    # --- verdict-record fields (R26.12/B20, O-8) -----------------------------
    # The record B21 mints per merged PR, reconciled onto this gate-run shape
    # rather than carried by a second model: ``reviewer`` becomes the identity
    # that recorded the verdict (the gate agent's, or ``override:<reason>``)
    # and ``pr_refs`` holds ``pr_url``. All six are optional because nothing
    # writes them yet — B21 is the writer — and every gate-run record on file
    # today carries none of them.
    pr_url: Optional[str] = None
    pr_number: Optional[int] = None
    verdict_source: Optional[Literal["body", "comment", "override"]] = None
    bead_id: Optional[str] = None
    change_kind: Optional[Literal["structural", "behavioral", "emergency"]] = None
    merged_at: Optional[AwareDatetime] = None

    @model_validator(mode="after")
    def pr_url_must_agree_with_pr_refs_and_pr_number(self) -> "DevReleaseContent":
        """A verdict's PR reference may not disagree with itself.

        ``pr_url`` and ``pr_number`` are two spellings of the same fact; left
        unchecked, a record could name one PR in ``pr_url``/``pr_number`` and
        a different one in ``pr_refs``, which is precisely the ambiguity O-8
        exists to foreclose.
        """
        if self.pr_url is None:
            return self
        if self.pr_url not in self.pr_refs:
            raise ValueError("pr_url must be present in pr_refs")
        pull_marker = "/pull/"
        idx = self.pr_url.rfind(pull_marker)
        match = re.match(r"\d+", self.pr_url[idx + len(pull_marker):]) if idx != -1 else None
        if match is None:
            raise ValueError("pr_url must contain '/pull/<number>'")
        if self.pr_number != int(match.group()):
            raise ValueError(
                "pr_number must equal the integer after the final '/pull/' in pr_url"
            )
        return self

    @model_validator(mode="after")
    def a_merge_verdict_needs_its_required_suites_run(self) -> "DevReleaseContent":
        """A clean verdict may not rest on suites that were never executed.

        Without this, "merge" can be reported over a required suite sitting at
        ``not-run`` — which is precisely the false green this platform keeps
        having to fix.
        """
        if self.verdict != "merge":
            return self
        by_suite = {r.suite: r for r in self.results}
        for suite in self.required_suites:
            result = by_suite.get(suite)
            if result is None or result.outcome in ("not-run", "fail"):
                raise ValueError(
                    f"verdict 'merge' requires suite {suite!r} to have run and passed"
                )
        return self


class DevFindingContent(BaseModel):
    """A defect, enhancement or security issue raised at the gate — ``dev.finding``.

    One type with a ``kind``, following ``dev.note``, rather than three types.
    Findings share a lifecycle and a disposition; splitting them would triple
    the schema to express a single field.
    """

    model_config = ConfigDict(extra="allow")

    kind: Literal["bug", "enhancement", "security"]
    disposition: Literal["blocking", "backlog"]
    severity: Literal["high", "medium", "low"]
    summary: str
    reproduction: Optional[str] = None
    evidence: Optional[str] = None

    @model_validator(mode="after")
    def blocking_findings_must_carry_evidence(self) -> "DevFindingContent":
        """Blocking a release is a claim, and claims here carry evidence.

        Two audit rounds on this repo produced confident, wrong findings that
        would each have blocked a merge. Requiring evidence does not make a
        finding correct, but it makes it checkable.
        """
        if self.disposition == "blocking" and not (self.evidence or "").strip():
            raise ValueError("a blocking finding must carry evidence")
        return self


DEV_TYPE_SCHEMAS: Dict[str, type[BaseModel]] = {
    "note": DevNoteContent,
    "task": DevTaskContent,
    "design": DevDesignContent,
    "release": DevReleaseContent,
    "finding": DevFindingContent,
}

for _dev_bead_type, _dev_model_cls in DEV_TYPE_SCHEMAS.items():
    NAMESPACE_TYPE_SCHEMAS.register("dev", _dev_bead_type, _dev_model_cls)
del _dev_bead_type, _dev_model_cls


SourceClass = Literal["authored", "derived", "observed"]

SOURCE_CLASSES: frozenset[str] = frozenset({"authored", "derived", "observed"})

# Historical beads written before this field existed carry no source_class at
# all. They read as authored rather than invalid — the conservative default:
# nothing gets automation-writable just because it predates the contract. The
# backfill (scripts/arch-source-class-backfill.py) makes this classification
# explicit content; this default is what covers the gap until it runs.
DEFAULT_SOURCE_CLASS: SourceClass = "authored"


class ArchSourceClassContent(BaseModel):
    """The precedence declaration every ``arch.*`` bead content carries.

    Validated ahead of (and in addition to) any per-type model in
    :func:`validate_bead_content`, so the enum is enforced namespace-wide —
    including for an ``arch`` type with no dedicated content model yet. Every
    modelled Arch*Content class below also declares this field directly
    (rather than relying solely on this pre-check) so it survives a
    strict/`extra="forbid"` model like :class:`ArchObservationContent`.
    """

    model_config = ConfigDict(extra="allow")

    source_class: SourceClass = DEFAULT_SOURCE_CLASS


def read_source_class(content: Dict[str, Any]) -> str:
    """The effective source_class of stored content, defaulting historical data.

    Treats a missing *or* unrecognized stored value as ``authored`` — the same
    conservative default the schema itself applies to a fresh write. This is
    what makes a pre-migration bead (or a row a future rollback partially
    reverted) read as authored instead of raising.
    """
    value = (content or {}).get("source_class")
    return value if value in SOURCE_CLASSES else DEFAULT_SOURCE_CLASS


def content_declares_source_class(content: Dict[str, Any]) -> bool:
    """Whether ``content`` itself carries a recognized ``source_class`` value.

    Distinct from :func:`read_source_class`, which defaults a missing or
    unrecognized value to ``"authored"`` so historical content keeps
    validating. That default makes an undeclared bead indistinguishable, to a
    caller that only has the resolved string, from a fact a human genuinely
    declared ``authored`` — the distinction :func:`check_source_class_ownership`
    needs to tell "never overwritable by automation" apart from "not yet
    declared, so an automated writer should be told to declare it instead."
    """
    return (content or {}).get("source_class") in SOURCE_CLASSES


class ArchContentBase(BaseModel):
    """Common content contract for Stage 1/2 ``arch`` beads.

    ea-metamodel.md §3 makes these fields mandatory because the EA model is the
    lifecycle record, not prose. Type-specific models below stay additive with
    ``extra="allow"`` so authored fields like notes/debt/loc survive while the
    required architectural evidence is enforced.
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
    GitOps orphan is a real state that must be expressible — ``app.itop`` was
    exactly this on 2026-07-30, running 40 days under no Application and with
    no manifest in the repo. A model that cannot say so cannot report it.
    """

    model_config = ConfigDict(extra="allow")

    cluster: str
    namespace: str
    kind: str
    name: str
    manifest: Optional[str] = None
    managed_by: Literal["argocd", "deploy-workflow", "helm", "bootstrap", "none"]


class ArchWorkloadBinding(BaseModel):
    """A structured, locally checkable pointer to an external runtime (ea-metamodel.md §4.2).

    ``ea-conformance.py``'s ``check_workload`` and ``ea_reflect.py``'s
    ``_external_binding`` both already read exactly this shape — ``host``,
    ``compose_path``, ``service`` — from ``workload.binding`` on twenty-seven
    call sites between them. Declaring it here does not change what either
    script reads; it means a malformed binding is refused at write time
    (422) instead of arriving at those scripts as whatever the writer
    happened to spell, silently accepted by ``ArchWorkload``'s prior
    ``extra="allow"``.
    """

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

    Required on every ``arch.application``. The field exists to keep
    *unassessed* and *runs nowhere* apart: an object with no workload has not
    been looked at, while ``runtime: none`` is a dated finding. Only the second
    is safe to act on, and retirement acts on it.
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
    """One Gherkin-style criterion inside an ``arch.requirement`` bead.

    Criteria stay additive for future scanner metadata, but dated conformance
    measurements are forbidden recursively: they are observations, not
    requirement structure.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    given: str
    when: str
    then: str
    verification: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def criteria_do_not_embed_measurements(cls, value: Any) -> Any:
        paths = _forbidden_requirement_measurement_paths(value)
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

    The LifeOps registry (``docs/requirements/lifeops-requirements.json``) writes
    every one of its forty-three requirements' intent as ``{as_a, i_want,
    so_that}``, not prose — deliberately: it is how the Product Owner actually
    thinks about the requirement, and flattening it into a sentence would be a
    lossy rewrite of their own words for the schema's convenience.

    Closed (``extra="forbid"``), unlike most content models here that stay
    additive. This type exists to be the *second of exactly two* admitted
    shapes for ``rationale`` — if it tolerated stray keys, a near-miss shape
    (a typo'd field name, a stray ``rationale`` nested inside it) would quietly
    parse as a user story instead of being refused as the third shape it is.
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

    The content mirrors entries in ``docs/requirements/*.json`` plus the source
    registry id. It deliberately omits conformance/verdict fields because those
    are dated measurements and belong in ``arch.requirement_conformance`` beads,
    not requirement structure.

    ``rationale`` is a declared union of exactly two shapes — prose
    (``str``, what ``docs/requirements/platform-requirements.json`` writes) or a
    user story (:class:`ArchRequirementUserStoryRationale`, what every LifeOps
    entry writes) — not a third, free-form field alongside it. Two reasons this
    is a union on ``rationale`` rather than a separate ``user_story`` field with
    ``rationale`` still required prose: first, ``scripts/requirements-load.py``
    already mirrors the registry's ``user_story`` object onto ``rationale``
    verbatim (its one existing line of LifeOps-awareness), so the union is the
    only change that asks nothing of a script outside this app's boundary;
    second, a required prose ``rationale`` would force something to *write*
    that prose — either this schema fabricating a sentence the Product Owner
    never wrote, or a human transcribing forty-three of them by hand — and
    neither is this task's to perform. ``Any``/``dict`` was rejected for the
    same reason a third field was: it admits anything, including the exact
    near-miss shapes the closed :class:`ArchRequirementUserStoryRationale`
    exists to refuse.
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

    ``ArchRequirementContent``'s docstring, and ``_RELEASE_MEASUREMENT_KEYS`` below, both name
    ``arch.observation`` as the home for a requirement's dated conformance/verdict measurements.
    It never was: ``ArchObservationContent`` is closed (``extra="forbid"``) on ``observed_at`` and
    ``workload``, fields a requirement measurement does not have and cannot supply. Every write
    ``scripts/requirements-load.py`` ever attempted there was rejected on the fields it added and
    the two it omitted. This is the type both refusals actually name now.

    Follows ``ArchCiContent``'s precedent (see its docstring) rather than widening the closed
    observation shape: a new record that does not fit gets its own closed sibling type, not a
    relaxed version of the one that was closed on purpose. Deliberately NOT an
    :class:`ArchContentBase` subclass, for the same reason ``ArchCiContent`` is not one — a
    mechanically mirrored record has no ``owner``/``layer``/``evidence`` in the authored sense
    those fields exist for.

    Flat rather than nesting the measurement under a ``measurement`` key: the fields
    ``release-status.py`` reads (``requirement_id``, ``acceptance_criterion_id``, ``measured_at``,
    ``verdict``) are the payload, not metadata about it. ``verdict`` is the one field for what the
    source registries write inconsistently as ``conformance`` or ``verdict`` — the loader
    normalizes both onto this one substrate field, so a reader never has to know the registry used
    two names for one idea.

    ``source_class`` is locked to ``Literal["derived"]`` rather than defaulting the way most other
    arch types do, following ``ArchCiContent``'s precedent: the record is mechanically mirrored
    from ``docs/requirements/*.json`` by ``requirements-load.py``, neither authored in the
    substrate nor observed from the cluster, and a hand-created one is rejected at the schema layer
    (``POST /beads`` -> 422) instead of surviving until ``check_source_class_ownership`` catches it
    on a later ``PATCH``. See ``.factory/design.md``.
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


_PRINCIPLE_MEASUREMENT_KEYS = frozenset(
    {"measured_at", "verdict", "applied_count", "conformance"}
)


def _forbidden_principle_measurement_paths(
    value: Any, path: tuple[str, ...] = ()
) -> list[tuple[str, ...]]:
    """Find dated measurement fields embedded in principle structure."""
    if isinstance(value, dict):
        paths = []
        for key, child in value.items():
            child_path = (*path, str(key))
            if key in _PRINCIPLE_MEASUREMENT_KEYS:
                paths.append(child_path)
            paths.extend(_forbidden_principle_measurement_paths(child, child_path))
        return paths
    if isinstance(value, list):
        paths = []
        for index, child in enumerate(value):
            paths.extend(
                _forbidden_principle_measurement_paths(child, (*path, str(index)))
            )
        return paths
    return []


class ArchPrincipleStatusHistoryEntry(BaseModel):
    """One dated transition inside an ``arch.principle``'s ``status_history``.

    Every field is required: promotions earn their way and demotions explain
    themselves, so a transition without a reason is invalid by design
    (doctrine.md's ladder).
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
    """Validated shape for ``arch.principle`` bead content (doctrine.md).

    Mirrors the ``PRIN-NNN`` entries in ``docs/architecture/principles.md``: a
    statement, its rationale, provenance, and a status ladder with a dated,
    reasoned history. Deliberately omits staleness/application measurement
    fields (F-DCE-5): whether an adopted principle is actually applied is a
    dated ``arch.observation``, never principle structure.
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
        paths = _forbidden_principle_measurement_paths(value)
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

    Observation content is deliberately closed. The cluster reflector may record
    what it saw, but judgment fields like TIME disposition and lifecycle state
    must stay on reviewed architecture changes, not reflector writes. ``ref`` is
    optional so ad hoc reflector writes stay valid; reviewed observations use it
    as the loader's idempotency key.
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

    ea-metamodel.md §7: Manage Technical Services opens 2026-08-22, populated
    by observation only. Closed shape (``extra="forbid"``) like
    :class:`ArchObservationContent`, and deliberately NOT an
    :class:`ArchContentBase` subclass — an automated-only existence record has
    no ``owner``/``evidence``/``assessed_at`` in the authored sense those
    fields exist for.

    ``source_class`` is locked to ``Literal["observed"]`` rather than
    defaulting to it the way every other arch type's does. The S33-B2
    ownership contract (``check_source_class_ownership``) only fires on
    ``PATCH`` — a hand-created record, written the way every other arch type
    naturally is (``source_class`` omitted, or declared ``"authored"``),
    would otherwise sail through ``POST /beads`` with nothing to check yet.
    Locking the field here rejects that create at the schema layer instead,
    through the same ``_validate_content_or_422`` path every arch type
    already goes through. See ``.factory/design.md`` §2.

    ``kind`` is the concrete k8s resource kind kubectl reports (Deployment,
    StatefulSet, Namespace — matching :class:`ArchObservedWorkload`'s and
    :class:`ArchWorkloadObject`'s existing naming); ``ci_kind`` is the CMDB
    category this type exists to carry.
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

#: Measurement keys a release charter may never carry. A release states what
#: was *intended*; what actually happened for a cited requirement criterion is
#: an ``arch.requirement_conformance`` bead, exactly as ``ArchRequirementContent``
#: keeps dated verdicts out of requirement structure. ``actual_balance`` is
#: listed because it is the specific way this type would be tempted to grow a
#: second, drifting copy of the truth — it is computed from ``delivers`` edges
#: at read time (``scripts/release-status.py``), never stored.
_RELEASE_MEASUREMENT_KEYS = frozenset(
    {"conformance", "verdict", "measured_at", "measured_revision", "actual_balance"}
)


class ReleaseOutcome(BaseModel):
    """One thing that is tangibly different once a release lands.

    ``statement`` is written for the person the release is for, not for the
    person who built it: it is the line that ends up in the release notes. The
    ``work_class`` is here rather than on ``dev.task`` so that a task's class is
    *derived* through the outcome it delivers, and the two can never disagree.
    """

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
        """The same shape ``DevTaskContent.requirement_refs`` enforces.

        An outcome cites the criteria that prove it. A citation that cannot
        resolve reads as evidence and is not — the release-notes generator
        would render it as a measurement that never happened.
        """
        for ref in value:
            if not _REQUIREMENT_REF.match(ref):
                raise ValueError(
                    f"requirement ref {ref!r} must look like LO-CAT-004 or LO-CAT-004/AC-1"
                )
        return value


class ArchReleaseContent(BaseModel):
    """A release — the objective a body of work is aimed at, ``arch.release``.

    Deliberately NOT an :class:`ArchContentBase` subclass, for the reason
    :class:`ArchChangeContent` already gives: that base models configuration
    items, things with a layer and an owner and a lifecycle. A release is an
    envelope of changes against CIs, not a CI itself.

    This is the level above the sprint. Sprints are time boxes; the release
    holds the objective, the outcomes that objective decomposes into, and the
    balance of work classes the outcomes were *intended* to strike. Tasks reach
    it by a ``delivers`` edge, never by a string in their own content, because
    "everything in this release" has to be one indexed query.

    What it does not hold is what happened. ``declared_balance`` is intent;
    actual balance is computed from the edges at read time and recorded, if at
    all, as an ``arch.observation``. Pillar 10 draws that line for execution
    bookkeeping and ``ArchRequirementContent`` draws it for conformance
    verdicts; a release charter that accumulated its own measurements would be
    the third place to have to keep in sync.
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
        """``R<YY>.<NN>`` — a sequence within a year, not a sprint label.

        Bare ``R1``/``R2.2``/``R3`` are already in use in this repository as
        gitops-resilience *sprint* labels. A release id that could be read as
        one of those would make every cross-reference ambiguous.
        """
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
        """Two outcomes sharing an id make ``outcome_ref`` ambiguous.

        A task pointing at ``O-2`` has to resolve to exactly one statement, or
        the release notes attribute merged work to the wrong outcome.
        """
        seen: set[str] = set()
        for outcome in self.outcomes:
            if outcome.id in seen:
                raise ValueError(f"duplicate outcome id {outcome.id!r}")
            seen.add(outcome.id)
        return self

    @model_validator(mode="after")
    def declared_balance_is_a_legal_mix(self) -> "ArchReleaseContent":
        """Declared intent across the five work classes, summing to 100.

        The balance is declared so that "feature work crowded out risk and
        security" is answerable against something, rather than being an
        impression formed at the end. An empty declaration is allowed — a
        charter may decline to declare — but a partial or non-summing one is
        not, because it would read as a target while measuring nothing.
        """
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
            raise ValueError(
                f"declared_balance must sum to 100, got {total}"
            )
        return self

    @model_validator(mode="after")
    def declared_classes_are_reachable_through_an_outcome(self) -> "ArchReleaseContent":
        """A class declared at a non-zero share needs somewhere to land.

        Declaring 20% risk with no risk outcome is a target that cannot be hit
        by any task the charter admits — the report would show it permanently
        absent and the charter would never explain why.
        """
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


# Map of (arch) bead type -> Pydantic model. This covers ea-metamodel.md §2
# object types marked Stage 1 or Stage 2, plus the closed Track B observation
# type. Unknown arch types keep the documented pass-through behavior of
# validate_bead_content().
class ArchChangeContent(BaseModel):
    """An ITIL change record — ``arch.change``.

    Deliberately NOT an :class:`ArchContentBase` subclass. That base models
    configuration items: things with a layer, an owner and a lifecycle. A change
    is an *event against* a CI, so it carries the CI by a ``affects`` edge and
    keeps its own shape. Forcing it into the CI contract would give every change
    record a ``layer`` it has no meaning for.
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
    """A production incident — ``arch.incident`` (itsm-target-state.md §1).

    Deliberately NOT an :class:`ArchContentBase` subclass, for the reason
    :class:`ArchChangeContent` already gives: an incident is an *event against* a
    CI (carried by the ``affects`` edge to ``arch.application``), not a CI
    itself, so it has no ``layer``/``owner`` in the authored-CI sense.

    ``severity`` mirrors ``ALERT_INVENTORY``'s closed set
    (``apps/mcp-hub/src/tools/notify.py``'s ``AlertSeverity`` —
    urgent/actionable/informational) as a literal rather than an import: this
    schema's tree has no runtime dependency on mcp-hub, and one severity
    vocabulary expressed as two literal declarations is the existing pattern
    every other closed-set field in this file follows (see
    ``ArchChangeContent.change_type``).

    ``runbook_refs`` is the reachable-runbook promise
    (R26.05/O-2 — "a runbook can be reached from the incident that needed it")
    as a content field, not a new edge type: runbooks stay documents per the
    inventory's ruling in ``docs/architecture/bead-object-inventory.md``.
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
    """An accepted risk, its owner, and its review date — ``arch.risk`` (R26.09/O-5).

    Deliberately NOT an :class:`ArchContentBase` subclass, for the reason
    :class:`ArchPrincipleContent` already gives: a risk is a standing governance
    record with its own status ladder, not a configuration item with a ``layer``
    in the CI sense.

    ``severity`` reuses :class:`ArchApplicationContent`'s
    ``business_criticality`` vocabulary (``critical``/``high``/``medium``/``low``)
    rather than :class:`ArchIncidentContent`'s ``urgent``/``actionable``/
    ``informational`` — that set is ``ALERT_INVENTORY``'s alerting severity, a
    different measurement than risk criticality, and reusing it here would
    conflate the two.

    ``decision_ref`` is a free-text citation (e.g. ``"ARCHITECTURE.md#amendment-18"``)
    to the amendment or decision record that accepted the risk, not a bead ref:
    not every historical acceptance decision has a bead of its own. When one
    does, the ``accepted_by`` edge (``bead_rules.BEAD_LINK_TYPES``) carries the
    same fact as a queryable graph edge — declared twice deliberately, the
    pattern :class:`ArchImpact`'s docstring already gives for ``dev.design``.

    No ``applications`` content field: which applications a risk threatens is
    carried only by the ``threatens`` edge (PRIN-003), not duplicated in
    content the way :class:`ArchChangeContent`/:class:`ArchIncidentContent` do
    for a reason specific to when those types landed.
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
    """The release's own date window, read from its charter — ``time_box``.

    Both dates are ``None`` together when the charter has no ``target_at`` or
    the loader cannot parse it; B12 never fabricates one half of the pair.
    """

    model_config = ConfigDict(extra="forbid")

    opened_at: Optional[str] = None
    target_at: Optional[str] = None
    elapsed_pct: Optional[float] = None


class ArchReleaseHealthSignal(BaseModel):
    """One fired/pending/clear reading over the closed eleven-id signal set.

    ``fired`` is ``"unknown"`` rather than ``False`` when the input the signal
    depends on could not be read — see ``coverage.unreadable``.
    """

    model_config = ConfigDict(extra="forbid")

    id: Literal[_RELEASE_HEALTH_SIGNAL_IDS]  # type: ignore[valid-type]
    fired: Literal[True, False, "unknown"]
    pending: bool
    impact: Literal["low", "medium", "high"]
    evidence: str
    since: Optional[str] = None


class ArchReleaseHealthBalanceEntry(BaseModel):
    """One work class's declared share against what ``delivers`` edges show.

    ``actual_pct`` is ``None`` exactly when ``insufficient_sample`` is
    ``True`` (fewer than the policy's ``min_classified`` tasks bound) — never
    a computed zero standing in for "not enough data".
    """

    model_config = ConfigDict(extra="forbid")

    work_class: Literal["feature", "enabling", "blocking", "risk", "security"]
    declared_pct: int
    actual_count: int
    actual_pct: Optional[float] = None
    absent: bool
    insufficient_sample: bool


class ArchReleaseHealthOutcome(BaseModel):
    """One release outcome's delivery state, derived from bound tasks' states."""

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
    """Counts over ``arch.change`` beads bound to this release.

    Each ``int | None`` field here is ``None`` exactly when ``"changes"`` is
    listed in ``coverage.unreadable`` (``coverage.changes_read`` is
    ``False``) — never a computed zero standing in for "no changes found".
    """

    model_config = ConfigDict(extra="forbid")

    count: Optional[int] = None
    by_change_type: Dict[str, int] = Field(default_factory=dict)
    without_verdict_record: Optional[int] = None


class ArchReleaseHealthRework(BaseModel):
    """Task-level rework counts: a bound task counts once regardless of how
    many request-changes notes or requeues it collected.

    ``bound``/``request_changes_tasks``/``requeued_tasks``/``union``/
    ``superseded``/``rate`` are each ``None`` exactly when their source input
    is in ``coverage.unreadable`` — never a computed zero.
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
    """Whether the release's outcomes and criteria look closeable right now.

    ``ready`` is ``"unknown"`` rather than ``False`` when a required input
    (outcomes, criteria, changes) could not be read.
    """

    model_config = ConfigDict(extra="forbid")

    ready: Literal[True, False, "unknown"]
    blockers: list[str] = Field(default_factory=list)


class ArchReleaseHealthQueue(BaseModel):
    """A snapshot of the queue-order file's claimable-age reading, by class.

    ``oldest_claimable_age_days_by_class`` is the literal string ``"unknown"``
    (not an empty dict) when ``"queue_order_file"`` is listed in
    ``coverage.unreadable`` (``coverage.queue_order_file_read`` is
    ``False``) -- the file was missing, stale, or unreadable.
    """

    model_config = ConfigDict(extra="forbid")

    oldest_claimable_age_days_by_class: Union[Dict[str, float], Literal["unknown"]]
    read_from: str
    computed_at: Optional[str] = None


class ArchReleaseHealthCoverage(BaseModel):
    """Which inputs this measurement actually read — the honesty ledger B12
    writes so every ``None``/``"unknown"`` elsewhere can be traced to a cause.

    ``notes`` carries free-text remarks B12 appends (e.g.
    ``"disposition_ignored: <worker>"``, ``"disposition_spent: <note_id>"``),
    never a count or a signal.
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
    entered ``accepted``* — the reconciler's accepted rule compares a later
    measurement's fired set against this snapshot to decide whether the
    disposition still covers what is firing now.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["accepted", "replanned", "rechartered"]
    until: str
    note_id: str
    recorded_by: str
    fired_ids: list[str] = Field(default_factory=list)


class ArchReleaseHealthContent(BaseModel):
    """A release's governed health posture — ``arch.release_health``
    (R26.12/O-5, dev.finding 934989e6 F-B).

    Deliberately closed (``extra="forbid"``), unlike
    :class:`ArchIncidentContent`/:class:`ArchRiskContent`'s ``extra="allow"``:
    those are envelopes over authored prose or governance records meant to
    grow new fields under review, while this type is a reconciler's
    derivation (B12/B13) with no authored-prose use case at all — an
    unvalidated extra field written by its one automated writer is exactly
    the drift-without-a-check dev.finding 934989e6 names. The precedent is
    :class:`ArchObservationContent`, the other automated-only closed arch
    model. See ``.factory/design.md``.

    This bead registers the shape and the six-state machine only; B12
    computes the content, B13 writes it. No field here is validated against
    a live release, task, or policy file -- that is the reconciler's job.
    """

    model_config = ConfigDict(extra="forbid")

    ref: str  # "health.<release_ref>" -- the loader's idempotency key.
    release_ref: str  # the arch.release this record measures (also a `measures` edge).
    measured_at: str  # ISO datetime of this measurement, B12's `now`.
    measured_revision: str  # repo revision the measurement read tasks/changes/conformances at.
    policy_revision: str  # blob sha of docs/releases/policy/health-policy.json as read.
    time_box: ArchReleaseHealthTimeBox
    urgency: Literal["low", "medium", "high", "unknown"]  # elapsed-fraction read of time_box.
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


AGENT_PROVENANCE_WORKERS = frozenset({"codex", "claude", "gemini"})


class BeadProvenance(BaseModel):
    """Shape for attribution on newly agent-authored beads.

    Existing beads carry ``{}`` or one-off migration metadata, so read models
    preserve stored provenance as data during the backfill window. ``BeadCreate``
    validates non-empty provenance and requires a full record for known agent
    writers.

    ``tokens``/``cost_usd`` are ``Optional`` — nullable, but still required
    keys (``extra="forbid"`` demands all six present) — so a worker run that
    reported no usage figures can say so with ``None`` instead of ``0``. Before
    this, every ``dev.task`` provenance record hardcoded ``0``/``0.0`` even
    though the workers behind it spend real money on every run: the one
    artefact whose purpose is attribution asserted every run spent nothing,
    indistinguishable from a run truly measured at zero (PC-INF-003/AC-2).

    Two alternatives were rejected in favour of nullable fields:
      * A sentinel value (e.g. ``tokens=-1``) collides with ``ge=0`` — loosening
        that bound to fit a marker weakens the one check that catches an
        actually-negative measurement, to paper over the same defect class the
        marker exists to fix.
      * A second boolean field (e.g. ``measured: bool``) duplicates what
        ``None`` already says for free, and adds a way for the two fields to
        disagree.
    Nullable is also the only one of the three that keeps every already-stored
    record valid unchanged: a concrete int/float still validates under
    ``Optional[int]``/``Optional[float]``, so no history needs migrating.
    Compare :func:`provenance_for_operator_action` in
    ``apps/factory-dispatcher/dispatch.py``, which keeps ``0``/``0.0`` — there,
    no worker ran at all, so ``model: "none"`` already carries the "not a
    measurement" distinction and a null would say the same thing twice.
    """

    model_config = ConfigDict(extra="forbid")

    worker: str = Field(min_length=1)
    model: str = Field(min_length=1)
    prompt_ref: str = Field(min_length=1)
    tokens: Optional[int] = Field(ge=0)
    cost_usd: Optional[float] = Field(ge=0)
    duration_s: float = Field(ge=0)


def _created_by_is_agent(created_by: str) -> bool:
    parts = re.split(r"[/:\s]+", created_by.lower())
    return any(part in AGENT_PROVENANCE_WORKERS for part in parts)


# ARCHITECTURE.md §3.3's dispatcher namespace: every identity a factory
# reconciler writes under (``change_apply.py``, ``ea_observation.py``, and the
# rest of ``apps/factory-dispatcher/activities/``) is prefixed
# ``factory-dispatcher/``. Nothing writing under that prefix is a human at a
# keyboard, regardless of whether the tail after the slash happens to also
# split into an ``AGENT_PROVENANCE_WORKERS`` part.
_FACTORY_DISPATCHER_WRITER_PREFIX = "factory-dispatcher/"


def is_automated_writer(created_by: str) -> bool:
    """Whether ``created_by`` names a factory AI worker, not a human or script.

    Two ways a writer is automated: it names one of the recognized agent
    provenance workers (:func:`_created_by_is_agent`, keyed on
    ``AGENT_PROVENANCE_WORKERS``), or it writes under the
    ``factory-dispatcher/`` namespace — a Temporal-owned unattended activity,
    per the dispatcher's own ``CREATED_BY`` constants. Neither predicate
    alone covered ``factory-dispatcher/ea-observer`` and its siblings:
    splitting on ``[/:\\s]+`` and matching against ``{codex, claude, gemini}``
    finds no match in ``{"factory-dispatcher", "ea-observer"}``, so a
    reconciler's own writes read as HUMAN and the ``authored`` class's human
    exemption opened for them (the defect this closes).

    Loader scripts like ``ea-load``/``requirements-load``/``principles-sync``/
    ``release-load`` are deliberately NOT automated by either predicate: they
    write under their own bare identity, not ``factory-dispatcher/``, and they
    mirror human-authored, source-controlled docs — blocking them from ever
    touching an authored fact would be backwards.
    """
    if created_by.lower().startswith(_FACTORY_DISPATCHER_WRITER_PREFIX):
        return True
    return _created_by_is_agent(created_by)


class SourceClassViolation(NamedTuple):
    """A rejected overwrite attempt, in the shape the 409 detail reports."""

    owning_class: str
    rejected_writer: str


def check_source_class_ownership(
    existing_source_class: str, owner: str, writer: str
) -> Optional[SourceClassViolation]:
    """Return a violation if ``writer`` may not overwrite this fact, else ``None``.

    ``owner`` is the bead's declared writer of record (``Bead.created_by``,
    fixed at creation). An ``authored`` fact is blocked only for an automated
    writer — any other identity, including one that didn't create it, may
    still write it. A ``derived``/``observed`` fact is writable only by the
    exact identity that declared itself the deriver/observer.
    """
    if existing_source_class == "authored":
        if is_automated_writer(writer):
            return SourceClassViolation(owning_class="authored", rejected_writer=writer)
        return None
    if writer != owner:
        return SourceClassViolation(owning_class=existing_source_class, rejected_writer=writer)
    return None


def check_source_class_admission(
    declared_class: str, writer: str
) -> Optional[SourceClassViolation]:
    """Return a violation if ``writer`` may not mint a bead claiming ``declared_class``.

    Distinct question from :func:`check_source_class_ownership`: that function
    governs OVERWRITING a fact that already carries a class (comparing the
    writer against the bead's own ``created_by``). This governs CLAIMING a
    class in the first place, against ``bead_rules.SOURCE_CLASS_WRITERS``.
    It fires on two doors, not one: a fresh ref (or no ref), where no existing
    bead gives ownership anything to compare against (OPS-86); and a write of
    any kind — POST or PATCH — that CHANGES the resolved class of a fact that
    already exists (OPS-90), because ownership evaluates the class a bead
    already carries and therefore reads an authored-to-observed relabel as a
    permitted authored overwrite. One rule, two doors, one enrollment map.

    ``HUMAN_EXEMPTION`` marks ``authored`` in the map: rather than an
    enumerated allowlist, any writer :func:`is_automated_writer` does not
    flag is admitted, mirroring ``check_source_class_ownership``'s existing
    authored rule. Every other declared class is a closed enrollment — an
    unlisted or empty enrollment refuses every writer, with no default
    admission for anyone, including that class's own real writer.
    """
    enrollment = SOURCE_CLASS_WRITERS.get(declared_class, frozenset())
    if enrollment is HUMAN_EXEMPTION:
        if is_automated_writer(writer):
            return SourceClassViolation(owning_class=declared_class, rejected_writer=writer)
        return None
    if writer not in enrollment:
        return SourceClassViolation(owning_class=declared_class, rejected_writer=writer)
    return None


def _validate_provenance_record(provenance: dict) -> dict:
    if provenance == {}:
        return provenance
    return BeadProvenance.model_validate(provenance).model_dump()


def validate_bead_content(namespace: str, bead_type: str, content: Dict[str, Any]) -> None:
    """Raise ``pydantic.ValidationError`` for modelled namespace/type content.

    Unknown namespaces and unknown types remain pass-through until their
    contracts harden — except the ``source_class`` reconciliation contract,
    which every ``arch`` bead accepts regardless of whether its type has a
    dedicated content model yet (checked first, ahead of any per-type model).

    Looks up ``NAMESPACE_TYPE_SCHEMAS`` only — this module never imports a
    namespace it does not itself define at module level (the lazy import in
    ``validate_finance_content`` is the one exception), so a namespace that registers
    itself from elsewhere (``finance``, from ``finance_schemas.py``) is only
    known here once something has actually caused that module to be
    imported. On the deployed server that already happened before this ever
    runs: ``main.py`` imports ``finance_schemas`` as an explicit composition
    root at process start, so registration does not depend on which request
    happens to be the first one that touches finance content.
    """
    if namespace == "arch":
        ArchSourceClassContent.model_validate(content)

    model_cls = NAMESPACE_TYPE_SCHEMAS.get(namespace, bead_type)
    if model_cls is None:
        return
    model_cls.model_validate(content)


def validate_finance_content(bead_type: str, content: Dict[str, Any]) -> None:
    """Raise ``pydantic.ValidationError`` if ``content`` doesn't match the
    Phase 6 model for ``bead_type``. No-op for types we haven't modelled.

    Routed callers translate the ValidationError into a 422. Has no
    production caller (``routes.py`` calls :func:`validate_bead_content`
    directly) — this import is a safety net so a caller that reaches this
    function without the app's composition root having already run (a
    script, a bare ``import schemas``) still gets finance registered before
    the lookup below, rather than a silent pass-through.
    """
    try:
        from . import finance_schemas  # noqa: F401 - registers finance's content models
    except ImportError:  # pragma: no cover - bare-module consumers
        import finance_schemas  # noqa: F401
    validate_bead_content("finance", bead_type, content)


def validate_dev_content(bead_type: str, content: Dict[str, Any]) -> None:
    """Raise ``pydantic.ValidationError`` if ``content`` doesn't match the
    model for a hardened ``dev`` bead type. No-op for unknown types.
    """
    validate_bead_content("dev", bead_type, content)


def validate_arch_content(bead_type: str, content: Dict[str, Any]) -> None:
    """Raise ``pydantic.ValidationError`` if ``content`` doesn't match the
    model for a Stage 1/2 ``arch`` bead type. No-op for unknown types.
    """
    validate_bead_content("arch", bead_type, content)


__all__ = [
    "ArchApplicationContent",
    "ArchCapabilityContent",
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
    "ArchRiskContent",
    "ArchServiceContent",
    "ArchSourceClassContent",
    "ARCH_TYPE_SCHEMAS",
    "BeadBase",
    "BeadCreate",
    "BeadEventRead",
    "BeadLinkCreate",
    "BeadLinkRead",
    "BEAD_LINK_TYPES",
    "BeadProvenance",
    "BeadRead",
    "BeadSearchHit",
    "BeadSearchRequest",
    "BeadUpdate",
    "check_source_class_admission",
    "check_source_class_ownership",
    "content_declares_source_class",
    "DEFAULT_SOURCE_CLASS",
    "DevNoteContent",
    "DevTaskBudget",
    "DevTaskContent",
    "DevTaskScope",
    "DevTaskVerification",
    "DEV_TYPE_SCHEMAS",
    "HUMAN_EXEMPTION",
    "is_automated_writer",
    "NamespaceSchemaRegistry",
    "NAMESPACE_TYPE_SCHEMAS",
    "read_source_class",
    "SourceClass",
    "SourceClassViolation",
    "SOURCE_CLASSES",
    "SOURCE_CLASS_WRITERS",
    "validate_arch_content",
    "validate_bead_content",
    "validate_candidate_bead",
    "validate_dev_content",
    "validate_finance_content",
]


class BeadBase(BaseModel):
    namespace: str
    type: str
    state: str
    parent_id: Optional[UUID4] = None
    context: dict = Field(default_factory=dict)
    content: dict = Field(default_factory=dict)
    confidence: Optional[float] = Field(default=None, ge=0, le=1)
    trust_tier: str
    provenance: dict = Field(default_factory=dict)
    created_by: str

class BeadCreate(BaseModel):
    namespace: str
    type: str
    state: str
    parent_id: Optional[UUID4] = None
    context: dict = Field(default_factory=dict)
    content: dict = Field(default_factory=dict)
    confidence: Optional[float] = Field(default=None, ge=0, le=1)
    trust_tier: str
    provenance: dict = Field(default_factory=dict)
    created_by: str

    @field_validator("provenance")
    @classmethod
    def provenance_has_declared_shape(cls, provenance: dict) -> dict:
        return _validate_provenance_record(provenance)

    @model_validator(mode="after")
    def agent_authored_beads_carry_provenance(self):
        if _created_by_is_agent(self.created_by) and not self.provenance:
            raise ValueError("agent-authored beads require a complete provenance record")
        return self


def validate_candidate_bead(
    namespace: str,
    bead_type: str,
    content: Dict[str, Any],
    provenance: Dict[str, Any],
    created_by: str,
    *,
    trust_tier: str = "user",
    state: str = "pending",
) -> None:
    """Raise ``pydantic.ValidationError`` iff ``POST /beads`` would reject this
    namespace/type/content/provenance/created_by combination.

    Runs the same two checks ``create_bead`` (``routes.py``) runs before it
    ever touches the database — ``BeadCreate`` (bead shape, provenance shape,
    and the agent-authorship requirement) and :func:`validate_bead_content`
    (the namespace's content schema) — against the identical model classes
    the route uses, not a re-description of them.

    Two callers this exists for: a caller holding a candidate bead can learn a
    write would be refused without attempting it, and a caller holding an
    already-stored bead (fetched, never written by this call) can run the
    same check to learn the store accepted something its own declared schema
    would now reject — turning that gap into something a script can assert
    on rather than something a later reader discovers by choking on it.
    """
    BeadCreate.model_validate(
        {
            "namespace": namespace,
            "type": bead_type,
            "state": state,
            "content": content,
            "trust_tier": trust_tier,
            "provenance": provenance,
            "created_by": created_by,
        }
    )
    validate_bead_content(namespace, bead_type, content)


class BeadRead(BeadBase):
    id: UUID4
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

class BeadUpdate(BaseModel):
    state: Optional[str] = None
    parent_id: Optional[UUID4] = None
    content: Optional[dict] = None
    context: Optional[dict] = None
    confidence: Optional[float] = Field(default=None, ge=0, le=1)
    created_by: str = "system"

class BeadTransition(BaseModel):
    """Request body for atomic compare-and-set state transition."""
    from_state: str = Field(min_length=1)
    to_state: str = Field(min_length=1)
    created_by: str = "system"

class BeadSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=4000)
    limit: int = Field(default=10, ge=1, le=50)
    namespace: Optional[str] = None
    type: Optional[str] = None

class BeadSearchHit(BaseModel):
    bead: BeadRead
    score: float

class BeadLinkCreate(BaseModel):
    """Request body for ``POST /beads/{source_id}/links``.

    The source is the path parameter, so it is deliberately absent here —
    a body that could disagree with the URL is a bug waiting to be filed.
    """

    target_id: UUID4
    link_type: str = Field(min_length=1, max_length=64)
    content: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("link_type")
    @classmethod
    def normalize_link_type(cls, value: str) -> str:
        # Edges are matched exactly in SQL, so "designs" and "Designs "
        # must not become two different edge kinds that look identical on
        # screen. Normalising at the gate keeps the index honest.
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("link_type must not be blank")
        if normalized not in BEAD_LINK_TYPES:
            allowed = ", ".join(sorted(BEAD_LINK_TYPES))
            raise ValueError(f"link_type must be one of: {allowed}")
        return normalized


class BeadLinkRead(BaseModel):
    id: UUID4
    source_id: UUID4
    target_id: UUID4
    link_type: str
    content: dict
    created_at: datetime
    created_by: str

    class Config:
        from_attributes = True


class BeadEventRead(BaseModel):
    id: UUID4
    bead_id: UUID4
    event_type: str
    from_state: Optional[str] = None
    to_state: Optional[str] = None
    payload: dict
    created_at: datetime
    created_by: str

    class Config:
        from_attributes = True
