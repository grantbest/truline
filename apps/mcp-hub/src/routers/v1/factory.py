from typing import Any, Dict, List, Literal, Optional
from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from tools import (
    factory_merge,
    factory_probe,
    factory_schedule_status,
    factory_status,
    finding_filing,
    impact,
    note_filing,
    task_filing,
)
from access_auth import current_client_identity, require_authenticated_scope

router = APIRouter()


class TaskRunnableQuery(BaseModel):
    task_id: str = Field(..., description="dev.task bead id")


@router.post(
    "/task_runnable",
    summary="Whether a dev.task can be claimed right now, and why not if it can't",
    operation_id="get_task_runnable",
)
def factory_task_runnable(req: TaskRunnableQuery) -> Dict[str, Any]:
    # Deliberately sync: factory_status does blocking disk and HTTP work, so
    # FastAPI must run this on its thread pool, not the event loop.
    #
    # require_authenticated_scope (not check_scope): this surface reads the
    # release graph and what can start (PC-TRU-002/AC-1). It must be
    # reachable by exactly the identities that reach the console today, and
    # by nobody who reaches mcp-hub directly without Traefik's ForwardAuth —
    # see access_auth.require_authenticated_scope's docstring.
    require_authenticated_scope("factory.read")
    return factory_status.task_runnability(req.task_id)


@router.get(
    "/schedule_status",
    summary="Whether the dispatch schedule is paused, its pause note, and recent run outcomes",
    operation_id="get_factory_schedule_status",
)
async def factory_schedule_status_route() -> Dict[str, Any]:
    # factory.read, the same scope task_runnable/release_delivery already
    # check -- a read of schedule state, never a write (S54-B/R26.09 O-10:
    # this bead is the read half only; the pause control is a separate bead
    # reserved to the Operator). Not excluded from the MCP tool surface -- see
    # .factory/design.md for the outer loop's decision and its reasoning.
    require_authenticated_scope("factory.read")
    return await factory_schedule_status.dispatch_schedule_status()


@router.get(
    "/probe",
    summary=(
        "Probe the console operator surface: read what the release index and "
        "factory-state panel claim, re-read the store and the schedule "
        "independently, and record the comparison (R26.09/O-7)"
    ),
    operation_id="get_console_surface_probe",
)
async def factory_probe_console_surface(
    release_ref: Optional[List[str]] = Query(
        None, description="Release ref(s) to check; omit for every non-terminal release"
    ),
) -> Dict[str, Any]:
    # factory.read -- a read-and-compare probe, same posture as
    # schedule_status/task_runnable/release_delivery. It writes exactly one
    # standing arch.observation (never a dev.task, never a dev.note), through
    # the shared substrate client with its own enrolled writer identity, not
    # through this route's caller identity -- see
    # apps/factory-dispatcher/probe_console_surface.py's own docstring.
    require_authenticated_scope("factory.read")

    # Guaranteed non-None: require_authenticated_scope raises 401 above
    # otherwise -- see factory_file_task's identical comment.
    identity = current_client_identity.get()
    if identity is None:
        raise RuntimeError(
            "current_client_identity is None after require_authenticated_scope "
            "succeeded -- that call raises 401 for exactly this case, so "
            "reaching here means the identity context was cleared between "
            "the two calls, not a real deployment shape."
        )

    return await factory_probe.run_console_surface_probe(
        release_refs=release_ref, client_identity=identity.client
    )


class ImpactQuery(BaseModel):
    kind: Literal["change", "application", "ci"] = Field(
        ..., description="The EA object type to compute the impact of"
    )
    ref: str = Field(..., min_length=1, description="content.ref of the arch.<kind> bead")


@router.post(
    "/impact",
    summary=(
        "What a change, application or CI affects, over the graph's typed edges "
        "(affects/depends_on/consumes) -- with the coverage the answer is "
        "computed on, stated beside it (R26.09/O-4, PC-ASR-002/AC-1)"
    ),
    operation_id="get_impact_analysis",
)
async def factory_impact(req: ImpactQuery) -> Dict[str, Any]:
    # factory.read -- a read over the EA graph, same posture task_runnable/
    # release_delivery/probe already have on this router. No route_map
    # exclusion needed for the MCP tool surface: unlike /factory/note this is
    # a plain read with no side effect a remote agent could misuse, so it
    # falls through to openapi_app.py's default MCPType.TOOL -- see
    # .factory/design.md §6.
    require_authenticated_scope("factory.read")
    try:
        return await impact.get_impact(req.kind, req.ref)
    except impact.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except impact.Unavailable as exc:
        # A failed/refused population read must never surface as "no bead
        # with that ref" (round-2 #1020 gate finding) -- see impact.py's
        # Unavailable docstring and .factory/design.md.
        raise HTTPException(status_code=503, detail=str(exc)) from exc


class ReleaseDeliveryQuery(BaseModel):
    release_ref: str = Field(..., description="Release ref, e.g. 'R26.01'")


@router.post(
    "/release_delivery",
    summary="How much of a release's declared work is delivered",
    operation_id="get_release_delivery",
)
def factory_release_delivery(req: ReleaseDeliveryQuery) -> Dict[str, Any]:
    # Deliberately sync: see factory_task_runnable. Same auth rationale.
    require_authenticated_scope("factory.read")
    return factory_status.release_delivery(req.release_ref)


@router.post(
    "/tasks",
    summary="File a dev.task bead from a JSON spec — the same shape file_task.py's CLI accepts",
    operation_id="file_dev_task",
)
def factory_file_task(spec: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    # Deliberately sync: file_task.file_spec does blocking HTTP work against
    # the substrate, same as factory_task_runnable/factory_release_delivery.
    #
    # require_authenticated_scope: filing a bead must require the same real
    # authentication substrate_proxy and task_runnable already do, not the
    # stdio/internal bypass check_scope grants — see that function's
    # docstring. `factory.write` is not granted to any client as of this
    # change (ships dark; see .factory/design.md and this bead's own PR body)
    # so this route is unreachable in production today regardless.
    require_authenticated_scope("factory.write")

    # require_authenticated_scope above already raises 401 when
    # current_client_identity.get() is None (see its own docstring — every
    # HTTP request that reaches this point passed _identity_logging_dispatch,
    # which always sets an identity). So None here is not a deployment shape
    # to fall back from -- it would mean that guarantee broke between the two
    # calls. Raise loudly (PRIN-008) rather than recording a hard-coded
    # "unknown" worker as this bead's provenance (PRIN-015).
    identity = current_client_identity.get()
    if identity is None:
        raise RuntimeError(
            "current_client_identity is None after require_authenticated_scope "
            "succeeded -- that call raises 401 for exactly this case, so "
            "reaching here means the identity context was cleared between "
            "the two calls, not a real deployment shape."
        )
    created_by = identity.client

    try:
        bead = task_filing.file_dev_task(spec, created_by)
    except task_filing.FilingRefused as exc:
        # Same reason text file_task.py's CLI would raise as SystemExit —
        # PC-FAC-001/AC-5, reached from this second surface.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except task_filing.Unavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    content = bead.get("content") or {}
    return {
        "status": "ok",
        "id": bead.get("id"),
        "lane": content.get("lane"),
        "title": content.get("title"),
        "risk_class": content.get("risk_class"),
        "state": bead.get("state", "pending"),
    }


@router.post(
    "/findings",
    summary="File a dev.finding bead from a JSON spec — the same shape file_finding.py's CLI accepts",
    operation_id="file_dev_finding",
)
def factory_file_finding(spec: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    # Deliberately sync: finding_filing.file_dev_finding does blocking HTTP
    # work against the substrate, same as factory_file_task.
    #
    # require_authenticated_scope: same real authentication substrate_proxy/
    # task_runnable/factory_file_task already require, not the stdio/internal
    # bypass check_scope grants — see that function's docstring. `factory.write`
    # is not granted to any client as of this change (ships dark; see
    # .factory/design.md and this bead's own PR body) so this route is
    # unreachable in production today regardless.
    require_authenticated_scope("factory.write")

    # require_authenticated_scope above already raises 401 when
    # current_client_identity.get() is None (see its own docstring — every
    # HTTP request that reaches this point passed _identity_logging_dispatch,
    # which always sets an identity). So None here is not a deployment shape
    # to fall back from -- it would mean that guarantee broke between the two
    # calls. Raise loudly (PRIN-008) rather than recording a hard-coded
    # "unknown" worker as this bead's provenance (PRIN-015).
    identity = current_client_identity.get()
    if identity is None:
        raise RuntimeError(
            "current_client_identity is None after require_authenticated_scope "
            "succeeded -- that call raises 401 for exactly this case, so "
            "reaching here means the identity context was cleared between "
            "the two calls, not a real deployment shape."
        )
    created_by = identity.client

    # prompt_ref names the client, never the hub (this bead's AC-4): a
    # caller's own value passes through unchanged, and silence reads as
    # exactly where the finding came from rather than as an empty required
    # field the filer would otherwise refuse on.
    spec_with_prompt_ref = dict(spec)
    spec_with_prompt_ref["prompt_ref"] = spec.get("prompt_ref") or f"gateway/{identity.client}"

    try:
        result = finding_filing.file_dev_finding(spec_with_prompt_ref, created_by)
    except finding_filing.FilingRefused as exc:
        # Same reason text file_finding.py's CLI would raise as SystemExit.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except finding_filing.PartialWrite as exc:
        # The bead WAS created (exc.id) but failed to reach its disposition
        # state -- never collapse this into a 422 with file_finding.py's
        # bare SystemExit(1) detail ('1'), which would lose the id and read
        # as "nothing happened" when something did.
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except finding_filing.Unavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return {
        "status": "ok",
        "id": result.get("id"),
        "state": result.get("state"),
        "kind": result.get("kind"),
        "severity": result.get("severity"),
    }


class NoteCreateRequest(BaseModel):
    """The closed dev.note shape -- mirrors
    apps/substrate/src/schemas.py::DevNoteContent's own implied shape, so
    this route refuses exactly what the store would refuse (this bead's
    AC-3), not a second intake that accepts more and just moves the 422 one
    layer out.
    """

    parent_id: str = Field(..., description="dev.task bead id this note is filed against")
    kind: Literal["comment", "question", "answer", "status", "attachment", "review"]
    body: str
    blocking: Optional[bool] = None
    answers_ref: Optional[str] = None
    url: Optional[str] = None
    verdict: Optional[Literal["approve", "request-changes"]] = None
    # Never defaulted: apps/factory-dispatcher/guards.py::open_questions is
    # fail-closed on this field -- silence must read as "not released", never
    # as consent (this bead's AC-4). `None` here means only "the field's
    # default"; factory_file_note below forwards it only when the client's
    # own JSON body set the key (`model_fields_set`), never as a plain
    # attribute read that would turn "the client said nothing" into an
    # explicit `False`.
    releases_work: Optional[bool] = None

    @model_validator(mode="after")
    def validate_kind_specific_fields(self) -> "NoteCreateRequest":
        if not self.body.strip():
            raise ValueError("body must not be empty")

        if self.kind == "answer":
            if self.answers_ref is None:
                raise ValueError("answers_ref is required for answer notes")
        elif "answers_ref" in self.model_fields_set:
            raise ValueError("answers_ref is only valid for answer notes")

        if self.kind == "review":
            if self.verdict is None:
                raise ValueError("verdict is required for review notes")
        elif "verdict" in self.model_fields_set:
            raise ValueError("verdict is only valid for review notes")

        if self.kind == "attachment":
            if self.url is None:
                raise ValueError("url is required for attachment notes")
        elif "url" in self.model_fields_set:
            raise ValueError("url is only valid for attachment notes")

        if self.kind != "answer" and "releases_work" in self.model_fields_set:
            raise ValueError("releases_work is only valid for answer notes")

        if self.kind != "question" and self.blocking:
            raise ValueError("blocking is only valid for question notes")

        return self


@router.post(
    "/note",
    summary="File a dev.note bead against a dev.task -- comments, answers, status, review",
    operation_id="file_dev_note",
)
def factory_file_note(req: NoteCreateRequest) -> Dict[str, Any]:
    # Deliberately sync: note_filing.file_dev_note does blocking HTTP work
    # against the substrate, same as factory_file_task. Same auth rationale
    # -- require_authenticated_scope, the same "factory.write" scope task
    # filing uses (this bead's AC-1): the console reaches this identically
    # to how it reaches /tasks today. Excluded from the MCP tool surface
    # entirely (openapi_app.py's route_maps) -- see .factory/design.md for
    # why a `releases_work: true` answer note must never become something a
    # remote MCP agent can discover and call on its own blocking question.
    require_authenticated_scope("factory.write")

    # Guaranteed non-None: require_authenticated_scope raises 401 above
    # otherwise -- see factory_file_task's identical comment. Raise loudly
    # (PRIN-008) rather than recording a hard-coded "unknown" worker as this
    # bead's provenance (PRIN-015).
    identity = current_client_identity.get()
    if identity is None:
        raise RuntimeError(
            "current_client_identity is None after require_authenticated_scope "
            "succeeded -- that call raises 401 for exactly this case, so "
            "reaching here means the identity context was cleared between "
            "the two calls, not a real deployment shape."
        )
    created_by = identity.client

    fields: Dict[str, Any] = {}
    if req.blocking is not None:
        fields["blocking"] = req.blocking
    if req.answers_ref is not None:
        fields["answers_ref"] = req.answers_ref
    if req.url is not None:
        fields["url"] = req.url
    if req.verdict is not None:
        fields["verdict"] = req.verdict
    # Only forwarded when the client's own request body set the key --
    # this bead's AC-4. `req.releases_work` alone can't tell "omitted" from
    # "sent false"; `model_fields_set` can.
    if "releases_work" in req.model_fields_set:
        fields["releases_work"] = req.releases_work

    try:
        bead = note_filing.file_dev_note(
            req.parent_id, req.kind, req.body, created_by, identity.client_type, fields=fields
        )
    except note_filing.FilingRefused as exc:
        raise HTTPException(status_code=exc.status, detail=exc.body) from exc
    except note_filing.Unavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    content = bead.get("content") or {}
    return {
        "status": "ok",
        "id": bead.get("id"),
        "parent_id": bead.get("parent_id"),
        "kind": content.get("kind"),
        "state": bead.get("state", "active"),
    }


@router.post(
    "/prs/{number}/merge",
    status_code=202,
    summary=(
        "Start a merge-by-verdict workflow for a PR (R26.09/O-2) -- ships dark "
        "until a decision record grants factory.merge; see .factory/design.md"
    ),
    operation_id="start_pr_merge_on_verdict",
)
async def factory_start_pr_merge(number: int) -> Dict[str, Any]:
    # factory.merge is in access_auth.EXACT_MATCH_SCOPES: unlike every other
    # scope this router checks, the human wildcard '*' every authenticated
    # person receives (identity_headers) does NOT satisfy it -- only an
    # identity whose scope list names factory.merge explicitly passes. See
    # access_auth.EXACT_MATCH_SCOPES's own docstring and .factory/design.md.
    #
    # This route never reads GitHub and never merges: it only starts
    # MergeOnVerdictWorkflow on the factory-dispatcher's own worker, which
    # holds the `gh` credential and does the real work (fetch, gate_markers,
    # scripts/merge-pr.sh) -- and returns 202 immediately, so a phone request
    # never blocks behind that worker's `gh` calls, merge-pr.sh, and its
    # post-merge health check.
    require_authenticated_scope("factory.merge")

    # Guaranteed non-None: require_authenticated_scope raises 401 above
    # otherwise -- see factory_file_task's identical comment. Raise loudly
    # (PRIN-008) rather than recording a hard-coded requester.
    identity = current_client_identity.get()
    if identity is None:
        raise RuntimeError(
            "current_client_identity is None after require_authenticated_scope "
            "succeeded -- that call raises 401 for exactly this case, so "
            "reaching here means the identity context was cleared between "
            "the two calls, not a real deployment shape."
        )

    result = await factory_merge.start_merge_on_verdict(number, identity.client, "factory.merge")
    return {"status": "started", "pr": number, **result}


@router.get(
    "/prs/merge/{workflow_id}",
    summary="Read a merge-by-verdict workflow's recorded disposition",
    operation_id="get_pr_merge_disposition",
)
async def factory_get_pr_merge_disposition(workflow_id: str) -> Dict[str, Any]:
    # factory.read, not factory.merge: this reads a decision already made (or
    # in flight), the same posture task_runnable/release_delivery already
    # have -- not a second way to trigger the dangerous action. '*' satisfies
    # it, as it does every other factory.read route today.
    require_authenticated_scope("factory.read")
    try:
        return await factory_merge.get_merge_disposition(workflow_id)
    except factory_merge.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
