import re
import pytest
from datetime import datetime
from zoneinfo import ZoneInfo

import tools.ha as ha
from tools.ha import get_state, list_entities, get_history, get_office_intent_today

HA_BASE_URL = "http://homeassistant.home.svc.cluster.local:8123"
PERSON_HISTORY_RE = re.compile(
    f"^{HA_BASE_URL}/api/history/period/.*filter_entity_id=person.grant_best$"
)

@pytest.fixture
def mock_ha(httpx_mock):
    return httpx_mock

def add_neutral_intent_prereqs(mock_ha):
    mock_ha.add_response(
        url=f"{HA_BASE_URL}/api/states/input_boolean.skip_train_parking_today",
        json={"state": "off", "entity_id": "input_boolean.skip_train_parking_today"},
    )

def add_train_latch_negative(mock_ha):
    """No Metra-zone presence in today's history → the morning latch stays off.
    Mocks the zone (for friendly-name aliasing) and an empty person history."""
    mock_ha.add_response(
        url=f"{HA_BASE_URL}/api/states/zone.metra_stop",
        json={
            "state": "1",
            "entity_id": "zone.metra_stop",
            "attributes": {"friendly_name": "Metra stop - National"},
        },
    )
    mock_ha.add_response(url=PERSON_HISTORY_RE, json=[[]])

def add_commute_indicators_off(mock_ha):
    mock_ha.add_response(
        url=f"{HA_BASE_URL}/api/states/binary_sensor.on_way_to_city_monday_morning",
        json={"state": "off", "entity_id": "binary_sensor.on_way_to_city_monday_morning"},
    )
    mock_ha.add_response(
        url=f"{HA_BASE_URL}/api/states/input_boolean.on_my_way_to_city",
        json={"state": "off", "entity_id": "input_boolean.on_my_way_to_city"},
    )

def test_get_state_happy_path(mock_ha):
    mock_ha.add_response(
        url=f"{HA_BASE_URL}/api/states/person.grant_best",
        json={
            "entity_id": "person.grant_best",
            "state": "home",
            "attributes": {"friendly_name": "the Operator"},
            "last_changed": "2026-05-15T12:00:00+00:00"
        }
    )

    res = get_state("person.grant_best")
    assert "error" not in res
    assert res["state"] == "home"
    assert res["entity_id"] == "person.grant_best"

def test_get_state_error(mock_ha):
    mock_ha.add_response(
        url=f"{HA_BASE_URL}/api/states/non_existent",
        status_code=404
    )

    res = get_state("non_existent")
    assert "error" in res
    assert "404" in res["error"]

def test_list_entities_filtered(mock_ha):
    mock_ha.add_response(
        url=f"{HA_BASE_URL}/api/states",
        json=[
            {"entity_id": "person.grant_best", "state": "home", "attributes": {"friendly_name": "the Operator"}},
            {"entity_id": "zone.home", "state": "zoning", "attributes": {"friendly_name": "Home"}},
            {"entity_id": "light.kitchen", "state": "on", "attributes": {"friendly_name": "Kitchen Light"}}
        ]
    )

    res = list_entities(domain="person")
    assert isinstance(res, list)
    assert len(res) == 1
    assert res[0]["entity_id"] == "person.grant_best"

def test_get_history_happy_path(mock_ha):
    # Regex to match the timestamp in URL
    import re
    mock_ha.add_response(
        url=re.compile(f"^{HA_BASE_URL}/api/history/period/.*filter_entity_id=person.grant_best$"),
        json=[[
            {"state": "home", "last_changed": "2026-05-15T10:00:00+00:00"},
            {"state": "not_home", "last_changed": "2026-05-15T11:00:00+00:00"}
        ]]
    )

    res = get_history("person.grant_best", minutes=120)
    assert isinstance(res, list)
    assert len(res) == 2
    assert res[0]["state"] == "home"

def test_get_office_intent_manual_override(mock_ha):
    add_neutral_intent_prereqs(mock_ha)
    # Manual override ON
    mock_ha.add_response(
        url=f"{HA_BASE_URL}/api/states/input_boolean.office_today_chicago",
        json={"state": "on", "entity_id": "input_boolean.office_today_chicago"}
    )

    res = get_office_intent_today()
    assert res["going_to_office"] is True
    assert res["reason"] == "manual override"

def test_get_office_intent_skip_switch_wins(mock_ha):
    mock_ha.add_response(
        url=f"{HA_BASE_URL}/api/states/input_boolean.skip_train_parking_today",
        json={"state": "on", "entity_id": "input_boolean.skip_train_parking_today"},
    )

    res = get_office_intent_today()
    assert res["going_to_office"] is False
    assert res["reason"] == "parking skip switch on"

def test_get_office_intent_manual_off_is_neutral(mock_ha, monkeypatch):
    monkeypatch.setattr(ha, "_is_scheduled_city_commute", lambda now=None: False)
    add_neutral_intent_prereqs(mock_ha)
    mock_ha.add_response(
        url=f"{HA_BASE_URL}/api/states/input_boolean.office_today_chicago",
        json={"state": "off", "entity_id": "input_boolean.office_today_chicago"}
    )
    add_train_latch_negative(mock_ha)
    mock_ha.add_response(
        url=f"{HA_BASE_URL}/api/states/person.grant_best",
        json={"state": "home", "entity_id": "person.grant_best"},
    )

    res = get_office_intent_today()
    assert res["going_to_office"] is False
    assert res["reason"] == "currently home"
    assert res["signals"]["manual_override"] is False

def test_get_office_intent_city_indicator(mock_ha):
    add_neutral_intent_prereqs(mock_ha)
    mock_ha.add_response(
        url=f"{HA_BASE_URL}/api/states/input_boolean.office_today_chicago",
        json={"state": "off", "entity_id": "input_boolean.office_today_chicago"},
    )
    add_train_latch_negative(mock_ha)
    mock_ha.add_response(
        url=f"{HA_BASE_URL}/api/states/person.grant_best",
        json={"state": "not_home", "entity_id": "person.grant_best"},
    )
    mock_ha.add_response(
        url=f"{HA_BASE_URL}/api/states/binary_sensor.on_way_to_city_monday_morning",
        json={"state": "on", "entity_id": "binary_sensor.on_way_to_city_monday_morning"},
    )

    res = get_office_intent_today()
    assert res["going_to_office"] is True
    assert res["reason"] == "on way to city Monday morning indicator"

def test_get_office_intent_train_station_morning_latch(mock_ha, monkeypatch):
    # The phone was at the Metra stop at 06:55 CT (= 11:55 UTC) this morning and
    # has since moved on — current presence is irrelevant; the history latch
    # should still fire. Run time is 07:35 CT, after the 07:30 cutoff closed.
    add_neutral_intent_prereqs(mock_ha)
    mock_ha.add_response(
        url=f"{HA_BASE_URL}/api/states/input_boolean.office_today_chicago",
        json={"state": "off", "entity_id": "input_boolean.office_today_chicago"},
    )
    mock_ha.add_response(
        url=f"{HA_BASE_URL}/api/states/zone.metra_stop",
        json={
            "state": "1",
            "entity_id": "zone.metra_stop",
            "attributes": {"friendly_name": "Metra stop - National"},
        },
    )
    mock_ha.add_response(
        url=PERSON_HISTORY_RE,
        json=[[
            {"state": "home", "last_changed": "2026-06-17T11:40:00+00:00"},
            {"state": "Metra stop - National", "last_changed": "2026-06-17T11:55:00+00:00"},
            {"state": "not_home", "last_changed": "2026-06-17T12:20:00+00:00"},
        ]],
    )
    monkeypatch.setattr(
        ha,
        "_now_chicago",
        lambda: datetime(2026, 6, 17, 7, 35, tzinfo=ZoneInfo("America/Chicago")),
    )

    res = get_office_intent_today()
    assert res["going_to_office"] is True
    assert res["reason"] == "seen at Metra station this morning before cutoff"
    assert res["signals"]["train_station_morning"] is True

def test_get_office_intent_train_station_arrival_after_cutoff_does_not_latch(mock_ha, monkeypatch):
    # Phone reached the Metra stop at 07:50 CT (= 12:50 UTC) — after the 07:30
    # cutoff — so the morning latch must NOT fire (e.g. a late/off-schedule day).
    monkeypatch.setattr(ha, "_is_scheduled_city_commute", lambda now=None: False)
    add_neutral_intent_prereqs(mock_ha)
    mock_ha.add_response(
        url=f"{HA_BASE_URL}/api/states/input_boolean.office_today_chicago",
        json={"state": "off", "entity_id": "input_boolean.office_today_chicago"},
    )
    mock_ha.add_response(
        url=f"{HA_BASE_URL}/api/states/zone.metra_stop",
        json={
            "state": "1",
            "entity_id": "zone.metra_stop",
            "attributes": {"friendly_name": "Metra stop - National"},
        },
    )
    mock_ha.add_response(
        url=PERSON_HISTORY_RE,
        json=[[
            {"state": "Metra stop - National", "last_changed": "2026-06-17T12:50:00+00:00"},
        ]],
    )
    mock_ha.add_response(
        url=f"{HA_BASE_URL}/api/states/person.grant_best",
        json={"state": "not_home", "entity_id": "person.grant_best"},
    )
    add_commute_indicators_off(mock_ha)
    # Step 7 presence-fallback history scan (office zone) — empty.
    mock_ha.add_response(url=PERSON_HISTORY_RE, json=[[]])
    monkeypatch.setattr(
        ha,
        "_now_chicago",
        lambda: datetime(2026, 6, 17, 8, 15, tzinfo=ZoneInfo("America/Chicago")),
    )

    res = get_office_intent_today()
    assert res["going_to_office"] is False
    assert res["signals"]["train_station_morning"] is False

def test_get_office_intent_scheduled_monday_morning(mock_ha, monkeypatch):
    add_neutral_intent_prereqs(mock_ha)
    mock_ha.add_response(
        url=f"{HA_BASE_URL}/api/states/input_boolean.office_today_chicago",
        json={"state": "off", "entity_id": "input_boolean.office_today_chicago"},
    )
    mock_ha.add_response(
        url=f"{HA_BASE_URL}/api/states/person.grant_best",
        json={"state": "not_home", "entity_id": "person.grant_best"},
    )
    add_train_latch_negative(mock_ha)
    add_commute_indicators_off(mock_ha)
    monkeypatch.setattr(
        ha,
        "_now_chicago",
        lambda: datetime(2026, 6, 1, 6, 45, tzinfo=ZoneInfo("America/Chicago")),
    )

    res = get_office_intent_today()
    assert res["going_to_office"] is True
    assert res["reason"] == "away from home during scheduled Monday morning city commute"
    assert res["signals"]["scheduled_city_commute"] is True

def test_get_office_intent_scheduled_monday_morning_does_not_pay_at_home(mock_ha, monkeypatch):
    add_neutral_intent_prereqs(mock_ha)
    mock_ha.add_response(
        url=f"{HA_BASE_URL}/api/states/input_boolean.office_today_chicago",
        json={"state": "off", "entity_id": "input_boolean.office_today_chicago"},
    )
    add_train_latch_negative(mock_ha)
    mock_ha.add_response(
        url=f"{HA_BASE_URL}/api/states/person.grant_best",
        json={"state": "home", "entity_id": "person.grant_best"},
    )
    monkeypatch.setattr(
        ha,
        "_now_chicago",
        lambda: datetime(2026, 6, 1, 6, 45, tzinfo=ZoneInfo("America/Chicago")),
    )

    res = get_office_intent_today()
    assert res["going_to_office"] is False
    assert res["reason"] == "currently home"

def test_get_office_intent_presence_fallback(mock_ha, monkeypatch):
    monkeypatch.setattr(ha, "_is_scheduled_city_commute", lambda now=None: False)
    add_neutral_intent_prereqs(mock_ha)
    # Manual override missing or unknown
    mock_ha.add_response(
        url=f"{HA_BASE_URL}/api/states/input_boolean.office_today_chicago",
        status_code=404
    )
    add_train_latch_negative(mock_ha)
    # Person in office zone
    mock_ha.add_response(
        url=f"{HA_BASE_URL}/api/states/person.grant_best",
        json={"state": "zone.office_chicago", "entity_id": "person.grant_best"}
    )

    res = get_office_intent_today()
    assert res["going_to_office"] is True
    assert res["reason"] == "currently in office zone"

def test_get_office_intent_signals_shape_stable(mock_ha):
    # Same keys must be present regardless of which branch returns.
    expected_keys = {
        "manual_override",
        "parking_skip",
        "city_commute_indicator",
        "scheduled_city_commute",
        "local_time",
        "presence_now",
        "train_station_morning",
        "presence_seen_chicago_today",
    }

    # Branch 1: manual override on
    add_neutral_intent_prereqs(mock_ha)
    mock_ha.add_response(
        url=f"{HA_BASE_URL}/api/states/input_boolean.office_today_chicago",
        json={"state": "on", "entity_id": "input_boolean.office_today_chicago"},
    )
    assert set(get_office_intent_today()["signals"].keys()) == expected_keys
