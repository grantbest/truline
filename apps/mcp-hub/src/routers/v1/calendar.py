from typing import List, Dict, Any, Optional
from fastapi import APIRouter
from pydantic import BaseModel, Field
from access_auth import check_scope
from tools.google import get_calendar_events, get_upcoming_events

router = APIRouter()

class CalendarEventsRequest(BaseModel):
    date: Optional[str] = Field(
        None,
        description="ISO date (YYYY-MM-DD). Defaults to today.",
    )

@router.post("/events", summary="Calendar events for a single day across primary, family, and kids' calendars", operation_id="get_calendar_events")
async def calendar_events(req: CalendarEventsRequest) -> List[Dict[str, Any]]:
    check_scope("calendar.read")
    return get_calendar_events(req.date)

class CalendarUpcomingQuery(BaseModel):
    days_ahead: int = Field(7, ge=1, le=60)

@router.post("/upcoming", summary="Upcoming calendar events for the next N days", operation_id="get_upcoming_events")
async def calendar_upcoming(req: CalendarUpcomingQuery) -> List[Dict[str, Any]]:
    check_scope("calendar.read")
    return get_upcoming_events(req.days_ahead)
