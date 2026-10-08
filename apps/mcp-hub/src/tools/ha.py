import os
import httpx
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HA_BASE_URL = os.getenv("HA_BASE_URL", "http://homeassistant.home.svc.cluster.local:8123")
HA_TOKEN = os.getenv("HA_LONG_LIVED_TOKEN")
CHICAGO_TZ = ZoneInfo("America/Chicago")
WEEKDAYS = {
    "MON": 0,
    "MONDAY": 0,
    "TUE": 1,
    "TUESDAY": 1,
    "WED": 2,
    "WEDNESDAY": 2,
    "THU": 3,
    "THURSDAY": 3,
    "FRI": 4,
    "FRIDAY": 4,
    "SAT": 5,
    "SATURDAY": 5,
    "SUN": 6,
    "SUNDAY": 6,
}

def _get_ha_headers():
    return {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    }

def get_state(entity_id: str) -> dict:
    """Returns the current state of a Home Assistant entity.
    Returns: { entity_id, state, attributes, last_changed } or { error }
    """
    url = f"{HA_BASE_URL}/api/states/{entity_id}"
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(url, headers=_get_ha_headers())
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "entity_id": data.get("entity_id"),
                    "state": data.get("state"),
                    "attributes": data.get("attributes"),
                    "last_changed": data.get("last_changed"),
                }
            return {"error": f"HA returned {resp.status_code}: {resp.text}"}
    except Exception as e:
        return {"error": str(e)}

def list_entities(domain: str | None = None) -> list[dict] | dict:
    """Lists entities from Home Assistant, optionally filtered by domain.
    Returns: list of { entity_id, state, friendly_name } or { error }
    """
    url = f"{HA_BASE_URL}/api/states"
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(url, headers=_get_ha_headers())
            if resp.status_code == 200:
                entities = resp.json()
                if domain:
                    entities = [e for e in entities if e["entity_id"].startswith(f"{domain}.")]
                
                return [
                    {
                        "entity_id": e.get("entity_id"),
                        "state": e.get("state"),
                        "friendly_name": e.get("attributes", {}).get("friendly_name"),
                    }
                    for e in entities
                ]
            return {"error": f"HA returned {resp.status_code}: {resp.text}"}
    except Exception as e:
        return {"error": str(e)}

def get_history(entity_id: str, minutes: int = 60) -> list[dict] | dict:
    """Returns state changes for the entity over the last X minutes.
    Caps minutes at 1440 (24h).
    Returns: list of { state, last_changed } or { error }
    """
    if minutes > 1440:
        minutes = 1440
    
    start_time = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    url = f"{HA_BASE_URL}/api/history/period/{start_time}?filter_entity_id={entity_id}"
    
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(url, headers=_get_ha_headers())
            if resp.status_code == 200:
                # HA history returns a list of lists (one per entity)
                history = resp.json()
                if not history or not history[0]:
                    return []
                
                return [
                    {
                        "state": entry.get("state"),
                        "last_changed": entry.get("last_changed"),
                    }
                    for entry in history[0]
                ]
            return {"error": f"HA returned {resp.status_code}: {resp.text}"}
    except Exception as e:
        return {"error": str(e)}

def _now_chicago() -> datetime:
    return datetime.now(CHICAGO_TZ)

def _configured_commute_weekdays() -> set[int]:
    raw = os.getenv("OFFICE_COMMUTE_WEEKDAYS", "MON")
    days: set[int] = set()
    for part in raw.split(","):
        token = part.strip().upper()
        if not token:
            continue
        if token.isdigit():
            value = int(token)
            if 0 <= value <= 6:
                days.add(value)
            continue
        if token in WEEKDAYS:
            days.add(WEEKDAYS[token])
    return days or {0}

def _is_scheduled_city_commute(now: datetime | None = None) -> bool:
    local_now = now or _now_chicago()
    start_hour = int(os.getenv("OFFICE_COMMUTE_START_HOUR", "5"))
    end_hour = int(os.getenv("OFFICE_COMMUTE_END_HOUR", "10"))
    return (
        local_now.weekday() in _configured_commute_weekdays()
        and start_hour <= local_now.hour < end_hour
    )

def _train_station_cutoff() -> tuple[int, int]:
    """Parse TRAIN_STATION_CUTOFF ('HH:MM') into (hour, minute). Defaults 07:30."""
    raw = os.getenv("TRAIN_STATION_CUTOFF", "07:30").strip()
    try:
        hh, mm = raw.split(":", 1)
        hour, minute = int(hh), int(mm)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except (ValueError, AttributeError):
        pass
    return 7, 30

def _zone_presence_aliases(zone_entity_id: str) -> set[str]:
    """All states a person entity might report while inside `zone_entity_id`.

    HA is inconsistent about this: `zone.home` reports as "home", but most
    custom zones surface as their friendly_name (e.g. "Train Station"). We don't
    want to depend on which form the user's setup uses, so accept the entity_id,
    the bare slug, and the zone's live friendly_name. Matching is normalized to
    lowercase to absorb capitalization differences.
    """
    aliases = {zone_entity_id}
    if zone_entity_id.startswith("zone."):
        aliases.add(zone_entity_id[len("zone."):])
    zone = get_state(zone_entity_id)
    if "error" not in zone:
        friendly = (zone.get("attributes") or {}).get("friendly_name")
        if friendly:
            aliases.add(friendly)
    return {a.lower() for a in aliases if a}

def _seen_at_train_station_today(
    person_id: str, zone_entity_id: str, now: datetime | None = None
) -> bool:
    """Daily latch: did HA record the person inside the train-station zone at any
    point earlier today (Chicago) before the morning cutoff?

    A single scheduled poll can't catch the instant the phone crosses the
    geofence, so instead we scan today's history once the morning window has
    closed. This is robust to the phone having already moved on by run time
    (mid-commute on a moving train, or already downtown) — a current-position
    check would miss it, but history still holds the earlier station presence.
    """
    local_now = now or _now_chicago()
    cutoff = _train_station_cutoff()
    # Look back only to local midnight so yesterday's commute can't leak in.
    minutes_since_midnight = local_now.hour * 60 + local_now.minute + 1
    history = get_history(person_id, minutes=minutes_since_midnight)
    if not isinstance(history, list):
        return False
    aliases = _zone_presence_aliases(zone_entity_id)
    for entry in history:
        if (entry.get("state") or "").lower() not in aliases:
            continue
        changed_raw = entry.get("last_changed")
        if not changed_raw:
            continue
        try:
            changed = datetime.fromisoformat(changed_raw).astimezone(CHICAGO_TZ)
        except (ValueError, TypeError):
            continue
        if changed.date() != local_now.date():
            continue
        if (changed.hour, changed.minute) <= cutoff:
            return True
    return False

def get_office_intent_today() -> dict:
    """Returns a curated composite signal for office intent today.
    Checks an explicit skip switch, positive commute/office intent indicators,
    and presence fallback.
    Signals dict shape is stable across all return branches.
    """
    helper_id = os.getenv("HA_OFFICE_TODAY_ENTITY", "input_boolean.office_today_chicago")
    skip_helper_id = os.getenv("HA_SKIP_PARKING_ENTITY", "input_boolean.skip_train_parking_today")
    commute_indicator_id = os.getenv(
        "HA_CITY_COMMUTE_INDICATOR_ENTITY",
        "binary_sensor.on_way_to_city_monday_morning",
    )
    commute_manual_id = os.getenv("HA_CITY_COMMUTE_MANUAL_ENTITY", "input_boolean.on_my_way_to_city")
    person_id = os.getenv("HA_PERSON_ENTITY", "person.grant_best")
    office_zone = os.getenv("HA_OFFICE_ZONE_ENTITY", "zone.office_chicago")
    train_station_zone = os.getenv("HA_TRAIN_STATION_ZONE_ENTITY", "zone.metra_stop")

    local_now = _now_chicago()
    signals: dict = {
        "manual_override": None,
        "parking_skip": None,
        "city_commute_indicator": None,
        "scheduled_city_commute": False,
        "local_time": local_now.isoformat(timespec="minutes"),
        "presence_now": None,
        "train_station_morning": None,
        "presence_seen_chicago_today": False,
    }

    # 1. Explicit kill switch. Unlike office_today_chicago, this is the only
    # default-off helper that is allowed to veto payment.
    skip = get_state(skip_helper_id)
    if "error" not in skip:
        skip_state = skip.get("state")
        signals["parking_skip"] = skip_state == "on"
        if skip_state == "on":
            return {
                "going_to_office": False,
                "reason": "parking skip switch on",
                "signals": signals,
            }

    # 2. Office-day helper. ON forces payment. OFF is neutral because HA
    # initializes YAML-managed input_booleans to off, and treating that default
    # as a veto prevents the automatic fallback from ever running.
    helper = get_state(helper_id)
    if "error" not in helper:
        helper_state = helper.get("state")
        if helper_state == "on":
            signals["manual_override"] = True
            return {
                "going_to_office": True,
                "reason": "manual override",
                "signals": signals,
            }
        if helper_state == "off":
            signals["manual_override"] = False

    # 3. Train-station morning latch. The single most reliable office signal: if
    # HA saw the phone in the Metra zone at any point before this morning's
    # cutoff, we caught the train — pay parking. Checked against today's history
    # (not current position) because by the time this runs the window has closed
    # and the phone has usually moved on, so a point-in-time check would miss it.
    signals["train_station_morning"] = _seen_at_train_station_today(
        person_id, train_station_zone, local_now
    )
    if signals["train_station_morning"]:
        return {
            "going_to_office": True,
            "reason": "seen at Metra station this morning before cutoff",
            "signals": signals,
        }

    # 4. Presence — current zone. If HA still says home, do not pay. If HA says
    # not_home during the Monday commute window, treat that as "toward Chicago".
    person = get_state(person_id)
    if "error" not in person:
        signals["presence_now"] = person.get("state")
    if signals["presence_now"] == office_zone:
        return {
            "going_to_office": True,
            "reason": "currently in office zone",
            "signals": signals,
        }

    if signals["presence_now"] == "home":
        return {
            "going_to_office": False,
            "reason": "currently home",
            "signals": signals,
        }

    # 5. Positive commute indicator. This gives HA a visible, human-readable
    # "on my way to the city Monday morning" signal while keeping GPS as only
    # one of several inputs. Because the home check above has already run, this
    # can only pay when HA does not currently place the Operator at home.
    commute_indicator = get_state(commute_indicator_id)
    if "error" not in commute_indicator:
        indicator_on = commute_indicator.get("state") == "on"
        signals["city_commute_indicator"] = indicator_on
        if indicator_on:
            return {
                "going_to_office": True,
                "reason": "on way to city Monday morning indicator",
                "signals": signals,
            }

    commute_manual = get_state(commute_manual_id)
    if "error" not in commute_manual:
        manual_commute_on = commute_manual.get("state") == "on"
        signals["city_commute_indicator"] = manual_commute_on
        if manual_commute_on:
            return {
                "going_to_office": True,
                "reason": "manual on-way-to-city indicator",
                "signals": signals,
            }

    # 6. Schedule fallback. This is only positive if the phone has already left
    # home; it avoids charging parking while the Operator is still at home at 06:45.
    signals["scheduled_city_commute"] = _is_scheduled_city_commute(local_now)
    if signals["scheduled_city_commute"] and signals["presence_now"] == "not_home":
        return {
            "going_to_office": True,
            "reason": "away from home during scheduled Monday morning city commute",
            "signals": signals,
        }

    # 7. Presence fallback — last 4h of history.
    history = get_history(person_id, minutes=240)
    if isinstance(history, list):
        signals["presence_seen_chicago_today"] = any(
            entry.get("state") == office_zone for entry in history
        )

    if signals["presence_seen_chicago_today"]:
        return {
            "going_to_office": True,
            "reason": "seen in office zone recently",
            "signals": signals,
        }

    return {
        "going_to_office": False,
        "reason": "no office presence signals and no manual override",
        "signals": signals,
    }
