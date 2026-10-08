import os
from datetime import datetime, timedelta
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

def get_google_service():
    client_id = os.environ.get("MCP_HUB_CLIENT")
    client_secret = os.environ.get("MCP_HUB_SECRET")
    refresh_token = os.environ.get("MCP_HUB_REFRESH_TOKEN")
    
    if not all([client_id, client_secret, refresh_token]):
        raise ValueError("Missing Google OAuth credentials in environment")

    creds = Credentials(
        None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
    )
    
    if not creds.valid:
        creds.refresh(Request())
        
    return build("calendar", "v3", credentials=creds)

def get_calendar_events(date: str = None, calendars: list[str] = ["primary", "Best Family", "2014 Dynamite 25/26"]) -> list[dict]:
    """Get calendar events for a given date across specified calendars.
    date format: YYYY-MM-DD (defaults to today)
    """
    service = get_google_service()
    
    if date:
        start_dt = datetime.strptime(date, "%Y-%m-%d")
    else:
        start_dt = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    
    end_dt = start_dt + timedelta(days=1)
    
    time_min = start_dt.isoformat() + "Z"
    time_max = end_dt.isoformat() + "Z"
    
    all_events = []
    
    # First, list all calendars to find the IDs for named calendars
    calendar_list = service.calendarList().list().execute()
    calendar_map = {entry['summary']: entry['id'] for entry in calendar_list.get('items', [])}
    calendar_map['primary'] = 'primary'
    
    for cal_name in calendars:
        cal_id = calendar_map.get(cal_name)
        if not cal_id:
            continue
            
        events_result = service.events().list(
            calendarId=cal_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        
        events = events_result.get('items', [])
        for event in events:
            all_events.append({
                "calendar": cal_name,
                "summary": event.get("summary"),
                "start": event.get("start", {}).get("dateTime") or event.get("start", {}).get("date"),
                "end": event.get("end", {}).get("dateTime") or event.get("end", {}).get("date"),
                "location": event.get("location")
            })
            
    return sorted(all_events, key=lambda x: x['start'])

def get_upcoming_events(days_ahead: int = 7, calendars: list[str] = ["primary", "Best Family", "2014 Dynamite 25/26"]) -> list[dict]:
    """Get all events in the next N days across all specified calendars."""
    service = get_google_service()
    
    start_dt = datetime.now()
    end_dt = start_dt + timedelta(days=days_ahead)
    
    time_min = start_dt.isoformat() + "Z"
    time_max = end_dt.isoformat() + "Z"
    
    all_events = []
    
    calendar_list = service.calendarList().list().execute()
    calendar_map = {entry['summary']: entry['id'] for entry in calendar_list.get('items', [])}
    calendar_map['primary'] = 'primary'
    
    for cal_name in calendars:
        cal_id = calendar_map.get(cal_name)
        if not cal_id:
            continue
            
        events_result = service.events().list(
            calendarId=cal_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        
        events = events_result.get('items', [])
        for event in events:
            all_events.append({
                "calendar": cal_name,
                "summary": event.get("summary"),
                "start": event.get("start", {}).get("dateTime") or event.get("start", {}).get("date"),
                "end": event.get("end", {}).get("dateTime") or event.get("end", {}).get("date")
            })
            
    return sorted(all_events, key=lambda x: x['start'])
