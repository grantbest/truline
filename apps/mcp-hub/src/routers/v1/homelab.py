from typing import List, Dict, Any
from fastapi import APIRouter
from pydantic import BaseModel, Field
from tools.homelab import (
    get_platform_status,
    get_grafana_alerts,
    query_beads as homelab_query_beads,
    get_litellm_spend,
)
from access_auth import check_scope
from routers.v1.types import SubstrateNamespace

router = APIRouter()

@router.post("/platform_status", summary="Rollout status of all K8s deployments", operation_id="get_platform_status")
async def homelab_platform_status() -> Dict[str, str]:
    check_scope("homelab.read")
    return {"status": get_platform_status()}

@router.post("/grafana_alerts", summary="Active firing alerts from Grafana", operation_id="get_grafana_alerts")
async def homelab_grafana_alerts() -> List[Dict[str, Any]]:
    check_scope("homelab.read")
    return get_grafana_alerts()

class HomelabBeadsQuery(BaseModel):
    namespace: SubstrateNamespace
    type: str | None = Field(None, description="Bead type, e.g. 'expense', 'bill', 'budget', 'digest'. Omit for all types.")
    state: str | None = Field(None, description="Bead state filter, e.g. 'pending', 'resolved'.")
    trust_tier: str | None = Field(None, description="Trust tier filter.")
    parent_id: str | None = Field(None, description="Restrict to children of this bead id.")
    created_after: str | None = Field(None, description="ISO8601 lower bound on created_at.")
    limit: int = 10
    offset: int = 0

# Same filter surface as the console's substrateClient.listBeads over the
# same Substrate /beads endpoint (see tools/homelab.py::query_beads) — not a
# separately-shaped read.
@router.post("/beads", summary="Recent beads from Substrate by namespace and type", operation_id="query_beads")
async def homelab_beads(req: HomelabBeadsQuery) -> List[Dict[str, Any]]:
    check_scope("homelab.read")
    return homelab_query_beads(
        req.namespace,
        req.type,
        req.limit,
        req.state,
        req.trust_tier,
        req.parent_id,
        req.created_after,
        req.offset,
    )

@router.post("/litellm_spend", summary="Current month token spend per model", operation_id="get_litellm_spend")
async def homelab_litellm_spend() -> Dict[str, float]:
    check_scope("homelab.read")
    return get_litellm_spend()
