"""End-to-end test of sensor.py through the real HA setup/event machinery.

Requires pytest-homeassistant-custom-component (see conftest.py); exercises
PLATFORM_SCHEMA, the DataUpdateCoordinator listeners, and RestoreEntity --
everything test_models.py/test_sequencer.py's plain-pytest Zone/Sequencer
tests don't touch.
"""

from datetime import timedelta

import homeassistant.util.dt as dt_util
from homeassistant.core import HomeAssistant, State
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    mock_restore_cache,
)

CONFIG = {
    "sensor": [
        {
            "platform": "climate_zone_sequencer",
            "min_cycle_duration": 0,
            "zones": [
                {
                    "id": 1,
                    "priority": 0,
                    "demand_heat_entity": "input_boolean.zone1_heat",
                    "demand_cool_entity": "input_boolean.zone1_cool",
                    "liveness_entity": "input_boolean.zone1_alive",
                    "ready_timeout": 60,
                },
                {
                    "id": 2,
                    "priority": 1,
                    "demand_heat_entity": "input_boolean.zone2_heat",
                    "demand_cool_entity": "input_boolean.zone2_cool",
                    "liveness_entity": "input_boolean.zone2_alive",
                    "ready_timeout": 60,
                },
            ],
        }
    ]
}


async def _setup(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "sensor", CONFIG)
    await hass.async_block_till_done()


async def _set(hass: HomeAssistant, entity_id: str, on: bool) -> None:
    # A refresh request made within the coordinator's 10s debounce
    # cooldown is deferred, not run -- advance past it so each _set
    # takes effect before the next one.
    hass.states.async_set(entity_id, "on" if on else "off")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    await hass.async_block_till_done()


async def test_holds_until_every_zone_has_reported(hass: HomeAssistant) -> None:
    await _setup(hass)
    assert hass.states.get("sensor.shared_mode").state == "cool"  # cold-start default

    await _set(hass, "input_boolean.zone1_heat", True)
    # zone 2 has never reported -- must still hold, not assume idle.
    assert hass.states.get("sensor.shared_mode").state == "cool"


async def test_flips_once_every_zone_is_ready(hass: HomeAssistant) -> None:
    await _setup(hass)
    await _set(hass, "input_boolean.zone1_heat", True)
    await _set(hass, "input_boolean.zone2_cool", False)
    assert hass.states.get("sensor.shared_mode").state == "heat"


async def test_contention_resolves_by_zone_priority(hass: HomeAssistant) -> None:
    await _setup(hass)
    await _set(hass, "input_boolean.zone1_heat", True)  # zone 1: priority 0
    await _set(hass, "input_boolean.zone2_cool", True)  # zone 2: priority 1
    assert hass.states.get("sensor.shared_mode").state == "heat"


async def test_restores_last_mode_across_restart(hass: HomeAssistant) -> None:
    mock_restore_cache(hass, [State("sensor.shared_mode", "heat")])

    await _setup(hass)
    # Neither zone has ever reported -- without restore this would be the
    # Sequencer's hardcoded cold-start Mode (cool), not the restored one.
    assert hass.states.get("sensor.shared_mode").state == "heat"
