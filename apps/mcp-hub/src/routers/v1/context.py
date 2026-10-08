from typing import Dict, Any
from fastapi import APIRouter
from access_auth import check_scope
from tools.context import get_current_context

router = APIRouter()

@router.post("/current", summary="Location, weather, and day context", operation_id="get_current_context")
async def context_current() -> Dict[str, Any]:
    check_scope("context.read")
    return get_current_context()
