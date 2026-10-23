"""End-to-end tests of sensor.py through HA's real setup and event machinery.

Needs pytest-homeassistant-custom-component (see conftest.py). Covers what
the plain-pytest model tests can't: observing zones from hass.states,
PLATFORM_SCHEMA, the coordinator, RestoreEntity, and the heartbeat.
"""

from datetime import timedelta

import homeassistant.util.dt as dt_util
from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, State
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    async_capture_events,
    async_fire_time_changed,
    mock_restore_cache,
)

SHARED = "sensor.shared_mode"


def _zone(name: str) -> dict:
    return {
        "name": name,
        "demand_heat": f"binary_sensor.{name}_heat",
        "demand_cool": f"binary_sensor.{name}_cool",
        "urgency_tier": f"sensor.{name}_tier",
        "comfort_priority": f"number.{name}_priority",
    }


CONFIG = {
    "sensor": [
        {
            "platform": "climate_zone_sequencer",
            "min_cycle_duration": 0,
            "zones": [_zone("kitchen"), _zone("bedroom")],
        }
    ]
}


def _set_zone(hass: HomeAssistant, name: str, heat: str, cool: str, tier="2", priority="50") -> None:
    hass.states.async_set(f"binary_sensor.{name}_heat", heat)
    hass.states.async_set(f"binary_sensor.{name}_cool", cool)
    hass.states.async_set(f"sensor.{name}_tier", tier)
    hass.states.async_set(f"number.{name}_priority", priority)


async def _settle(hass: HomeAssistant, seconds: float = 2) -> None:
    # Past the coordinator's 1s refresh debounce, twice: a deferred refresh
    # re-arms the cooldown when it finally runs.
    await hass.async_block_till_done()
    for offset in (seconds, seconds + 2):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=offset))
        await hass.async_block_till_done()


async def _setup(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "sensor", CONFIG)
    await _settle(hass)


def _zones(hass: HomeAssistant) -> dict[str, dict]:
    return {z["name"]: z for z in hass.states.get(SHARED).attributes["zones"]}


async def test_decides_from_current_states_at_startup(hass: HomeAssistant) -> None:
    # Demand already present before setup, and no entity changes afterwards.
    _set_zone(hass, "kitchen", heat="on", cool="off")
    _set_zone(hass, "bedroom", heat="off", cool="off")
    await _setup(hass)
    assert hass.states.get(SHARED).state == "heat"


async def test_heat_to_cool_handoff_is_not_lost(hass: HomeAssistant) -> None:
    _set_zone(hass, "kitchen", heat="on", cool="off")
    _set_zone(hass, "bedroom", heat="off", cool="off")
    await _setup(hass)
    # Cool turns on before heat turns off.
    hass.states.async_set("binary_sensor.kitchen_cool", "on")
    hass.states.async_set("binary_sensor.kitchen_heat", "off")
    await _settle(hass)
    assert hass.states.get(SHARED).state == "cool"
    assert _zones(hass)["kitchen"]["want"] == "cool"


async def test_unavailable_zone_keeps_its_last_known_want(hass: HomeAssistant) -> None:
    _set_zone(hass, "kitchen", heat="on", cool="off", tier="0")
    _set_zone(hass, "bedroom", heat="off", cool="on", tier="2")
    await _setup(hass)
    assert hass.states.get(SHARED).state == "heat"

    hass.states.async_set("binary_sensor.kitchen_heat", STATE_UNAVAILABLE)
    await _settle(hass)
    kitchen = _zones(hass)["kitchen"]
    assert kitchen["status"] == "stale"
    assert kitchen["want"] == "heat"
    assert hass.states.get(SHARED).state == "heat"  # still contending, not frozen or dropped


async def test_urgency_tier_outranks_comfort_priority(hass: HomeAssistant) -> None:
    _set_zone(hass, "kitchen", heat="on", cool="off", tier="2", priority="0")
    _set_zone(hass, "bedroom", heat="off", cool="on", tier="0", priority="99")
    await _setup(hass)
    assert hass.states.get(SHARED).state == "cool"


async def test_live_comfort_priority_reorders_contention(hass: HomeAssistant) -> None:
    _set_zone(hass, "kitchen", heat="on", cool="off", priority="10")
    _set_zone(hass, "bedroom", heat="off", cool="on", priority="20")
    await _setup(hass)
    assert hass.states.get(SHARED).state == "heat"

    hass.states.async_set("number.bedroom_priority", "5")
    await _settle(hass)
    assert hass.states.get(SHARED).state == "cool"


async def test_unchanged_mode_still_publishes_a_heartbeat(hass: HomeAssistant) -> None:
    _set_zone(hass, "kitchen", heat="on", cool="off")
    _set_zone(hass, "bedroom", heat="off", cool="off")
    await _setup(hass)
    changed = async_capture_events(hass, EVENT_STATE_CHANGED)

    await _settle(hass, seconds=31)
    await _settle(hass, seconds=62)
    beats = [e for e in changed if e.data["entity_id"] == SHARED]
    assert len(beats) >= 2
    assert all(e.data["new_state"].state == "heat" for e in beats)


async def test_diagnostic_attributes(hass: HomeAssistant) -> None:
    _set_zone(hass, "kitchen", heat="on", cool="off", tier="1", priority="40")
    _set_zone(hass, "bedroom", heat="off", cool="off")
    await _setup(hass)
    attrs = hass.states.get(SHARED).attributes
    assert _zones(hass)["kitchen"] == {
        "name": "kitchen",
        "status": "live",
        "want": "heat",
        "rank": [1.0, 40.0],
    }
    assert attrs["mode_change_allowed"] is True
    assert attrs["seconds_since_mode_change"] is not None


async def test_unreadable_rank_is_reported_as_none(hass: HomeAssistant) -> None:
    _set_zone(hass, "kitchen", heat="on", cool="off", tier=STATE_UNAVAILABLE)
    _set_zone(hass, "bedroom", heat="off", cool="off")
    await _setup(hass)
    assert _zones(hass)["kitchen"]["rank"] == [None, 50.0]


async def test_restores_last_mode_across_restart(hass: HomeAssistant) -> None:
    mock_restore_cache(hass, [State(SHARED, "heat")])
    await _setup(hass)
    # No zone has been seen, so nothing overrides the restored Mode.
    assert hass.states.get(SHARED).state == "heat"
