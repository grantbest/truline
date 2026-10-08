from typing import Dict, Any, Optional
from fastapi import APIRouter
from pydantic import BaseModel, Field
from access_auth import check_scope
from tools.ha import (
    get_state as ha_get_state,
    list_entities as ha_list_entities,
    get_history as ha_get_history,
    get_office_intent_today as ha_get_office_intent_today,
)

router = APIRouter()

class HAGetStateRequest(BaseModel):
    entity_id: str = Field(..., description="HA entity id, e.g. 'person.grant_best', 'zone.office_chicago'")

@router.post("/get_state", summary="Current state of a Home Assistant entity", operation_id="get_state")
async def ha_get_state_endpoint(req: HAGetStateRequest) -> Dict[str, Any]:
    check_scope("ha.read")
    return ha_get_state(req.entity_id)

class HAListEntitiesRequest(BaseModel):
    domain: Optional[str] = Field(
        None,
        description="HA domain filter (e.g., 'zone', 'person', 'input_boolean'). None = all entities.",
    )

@router.post("/list_entities", summary="List HA entities, optionally filtered by domain", operation_id="list_entities")
async def ha_list_entities_endpoint(req: HAListEntitiesRequest) -> Any:
    check_scope("ha.read")
    return ha_list_entities(req.domain)

class HAGetHistoryRequest(BaseModel):
    entity_id: str
    minutes: int = Field(60, ge=1, le=1440, description="Lookback window in minutes (capped at 24h).")

@router.post("/get_history", summary="HA entity state changes over the last N minutes", operation_id="get_history")
async def ha_get_history_endpoint(req: HAGetHistoryRequest) -> Any:
    check_scope("ha.read")
    return ha_get_history(req.entity_id, req.minutes)

@router.post("/get_office_intent_today", summary="Composite signal: is the Operator heading to Chicago office today?", operation_id="get_office_intent_today")
async def ha_get_office_intent_today_endpoint() -> Dict[str, Any]:
    check_scope("ha.read")
    return ha_get_office_intent_today()
