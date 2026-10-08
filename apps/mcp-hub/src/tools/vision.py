import logging
import os
import time
from typing import List, Dict, Any, Optional
from pydantic import BaseModel

from src.tools.cost import log_llm_cost, prompt_hash
from src.tools.finance import _call_substrate
from src.tools.litellm_client import LIFEOPS_DEFAULT_MODEL, chat_completion, extract_json_payload
from src.tools.provenance import build_provenance

logger = logging.getLogger(__name__)

class VisionExtraction(BaseModel):
    todos: List[str] = []
    events: List[Dict[str, Any]] = []
    expenses: List[Dict[str, Any]] = []
    summary: str


class StagedVisionExtraction(BaseModel):
    """Return shape for /vision/extract: the raw extraction plus the bead id
    that the LifeOps Console Vision Inbox will pick up for human review.
    """

    bead_id: str
    extraction: VisionExtraction


async def extract_from_image(image_base64: str) -> VisionExtraction:
    """Uses LiteLLM/Gemini Vision to extract data from an image."""
    LITELLM_API_KEY = os.environ.get("LITELLM_API_KEY")
    model = LIFEOPS_DEFAULT_MODEL

    prompt = """
    You are the Truline Vision Analyst. Analyze this image (handwritten note, receipt, or whiteboard).
    Extract the following into structured JSON:
    - todos: A list of tasks identified.
    - events: A list of calendar events with 'title' and 'date' (if identifiable).
    - expenses: A list of expenses with 'amount', 'vendor', and 'date' (if identifiable).
    - summary: A one-sentence summary of the image content.

    Respond ONLY with the JSON object.
    """

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_base64}"
                        }
                    }
                ]
            }
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "VisionExtraction",
                "schema": VisionExtraction.model_json_schema(),
            },
        },
    }

    headers = {"Authorization": f"Bearer {LITELLM_API_KEY}"}

    try:
        started = time.monotonic()
        result = await chat_completion(payload, headers=headers, timeout=60.0, required_url=True)
        body = result.body
        latency_ms = (time.monotonic() - started) * 1000.0
        raw_content = body["choices"][0]["message"]["content"]
        extraction = VisionExtraction.model_validate_json(
            extract_json_payload(raw_content, expected_type="object")
        )

        try:
            await log_llm_cost(
                model=model,
                usage=body.get("usage"),
                agent="vision/extract",
                context={"source": "vision/extract"},
                workflow_id=None,
                latency_ms=latency_ms,
                prompt_hash_value=prompt_hash(prompt),
                cost_usd=result.cost_usd,
            )
        except Exception as cost_exc:  # noqa: BLE001 — cost capture is best-effort
            logger.warning("Vision cost logging failed: %s", cost_exc)

        return extraction
    except Exception as e:
        logger.error(f"Vision extraction failed: {e}")
        raise e


async def stage_vision_extraction(
    extraction: VisionExtraction,
    *,
    source: str = "vision/extract",
    context: Optional[Dict[str, Any]] = None,
) -> str:
    """POST the extraction to Substrate as a ``vision.extraction`` bead in
    ``state="pending"``. Returns the bead id.

    Contract is mirrored by apps/lifeops-console/README.md — the Console's
    Vision Inbox lists exactly these beads. Approve/Reject is a state
    transition; downstream materialization (see materialization design note
    at the bottom of this module) is a separate concern.

    The bead is `trust_tier="unverified"` because the vision model is
    probabilistic and the whole point of the Inbox is the human review step.
    """

    bead = await _call_substrate(
        "create_bead",
        "vision",
        "extraction",
        "pending",
        extraction.model_dump(),
        "mcp-hub/vision",
        trust_tier="unverified",
        context=context or {},
        provenance=build_provenance(
            worker="mcp-hub/vision",
            model=LIFEOPS_DEFAULT_MODEL,
            prompt_ref=source,
        ),
    )
    bead_id = bead["id"]
    logger.info(
        "Staged vision.extraction bead %s (todos=%d, events=%d, expenses=%d)",
        bead_id,
        len(extraction.todos),
        len(extraction.events),
        len(extraction.expenses),
    )
    return bead_id


async def extract_and_stage(
    image_base64: str,
    *,
    context: Optional[Dict[str, Any]] = None,
) -> StagedVisionExtraction:
    """Convenience: extract + persist. The Console picks the bead up on its
    next poll. If extraction succeeds but persistence fails, the caller
    still sees the upstream error — we don't silently drop the extraction.
    """

    extraction = await extract_from_image(image_base64)
    bead_id = await stage_vision_extraction(extraction, context=context)
    return StagedVisionExtraction(bead_id=bead_id, extraction=extraction)


# ---------------------------------------------------------------------------
# Materialization design note (not yet implemented)
# ---------------------------------------------------------------------------
#
# When the Console PATCHes a vision.extraction bead from state="pending" to
# state="approved", we want todos/events/expenses materialized into proper
# typed beads:
#   - extraction.todos    -> personal.todo   (one bead each)
#   - extraction.events   -> personal.event  (one bead each)
#   - extraction.expenses -> finance.expense (one bead each)
#
# Open design questions before this is wired:
#
# 1. Trigger. Substrate has bead_events but no out-of-band notifier. Two
#    options:
#      a. Poll workflow: a Temporal schedule (every 1-2 min) queries
#         GET /beads?namespace=vision&type=extraction&state=approved and
#         filters out any with provenance.materialized_at set. Simple,
#         eventually consistent.
#      b. Push: the Console (or a new Substrate POST hook) starts the
#         workflow directly on approve. Faster, but couples the Console to
#         Temporal client config — extra surface for the browser app.
#    Recommendation: start with (a). It composes with the existing
#    scheduled-workflow pattern in temporal_worker.py and survives Console
#    downtime.
#
# 2. Idempotency. The materialize workflow must be safe to retry. Approach:
#    parent_id on each child bead = the extraction bead's id; on each
#    successful child write, PATCH the extraction's provenance with
#    {materialized_at: <ts>, child_ids: [...]}. The poll filter excludes
#    extractions that already have materialized_at, making re-runs no-ops.
#    Substrate's BeadUpdate accepts arbitrary context patches but
#    provenance is currently set-on-create only — that needs a small
#    schema extension (or piggy-back on context).
#
# 3. Schemas. The target bead types aren't validated by Substrate yet:
#    finance.expense passes through FINANCE_TYPE_SCHEMAS (no model), and
#    the entire personal namespace is unvalidated. Before materializing
#    we should land at least a minimal pydantic shape per type so the
#    Console can render them consistently. Suggested shapes:
#      personal.todo:    {text: str, due_date: Optional[date], source: str}
#      personal.event:   {title: str, date: date, time: Optional[str],
#                         location: Optional[str]}
#      finance.expense:  {amount: float, vendor: Optional[str],
#                         date: date, category: Optional[str],
#                         description: Optional[str]}
#    Where the vision payload is missing required fields (most likely
#    finance.expense.amount), the activity should skip the row and
#    surface it in the workflow result so the human can fix the source.
#
# 4. Reject path. When state="rejected", we still want a terminal record
#    (so the Inbox doesn't show it again). Nothing further is needed; the
#    Console's Reject button already sets state. The poll workflow only
#    looks at state="approved", so rejected rows are inert.
#
# Until this is built, approved beads simply sit in state="approved" with
# no downstream effect. That's intentional: it lets the Inbox be useful
# immediately (human review of vision output) without committing to the
# materialization design before its details are settled.
