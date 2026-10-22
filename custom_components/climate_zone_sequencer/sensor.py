"""HA platform entity for climate-zone-sequencer.

YAML/PLATFORM_SCHEMA, no config_flow. Demand and liveness come from
separate per-zone signals, never from a climate entity's own state.
Only the Sequencer's last Mode is restored; Zones always cold-start.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.components.sensor import PLATFORM_SCHEMA, SensorEntity
from homeassistant.const import STATE_ON
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .models import Mode, Sequencer, Zone

_LOGGER = logging.getLogger(__name__)

CONF_ZONES = "zones"
CONF_ZONE_ID = "id"
CONF_PRIORITY = "priority"
CONF_DEMAND_HEAT_ENTITY = "demand_heat_entity"
CONF_DEMAND_COOL_ENTITY = "demand_cool_entity"
CONF_LIVENESS_ENTITY = "liveness_entity"
CONF_READY_TIMEOUT = "ready_timeout"
CONF_MIN_CYCLE_DURATION = "min_cycle_duration"
CONF_DEFAULT_MODE = "default_mode"

# Catches staleness even absent any entity change.
POLL_INTERVAL = timedelta(seconds=30)

ZONE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ZONE_ID): int,
        vol.Required(CONF_PRIORITY): vol.Coerce(float),
        vol.Required(CONF_DEMAND_HEAT_ENTITY): cv.entity_id,
        vol.Required(CONF_DEMAND_COOL_ENTITY): cv.entity_id,
        vol.Required(CONF_LIVENESS_ENTITY): cv.entity_id,
        vol.Required(CONF_READY_TIMEOUT): cv.time_period,
    }
)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_ZONES): vol.All(cv.ensure_list, [ZONE_SCHEMA]),
        vol.Required(CONF_MIN_CYCLE_DURATION): cv.time_period,
        vol.Optional(CONF_DEFAULT_MODE): vol.Coerce(Mode),
    }
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: dict[str, Any],
    async_add_entities: AddEntitiesCallback,
    discovery_info: dict[str, Any] | None = None,
) -> None:
    zone_configs = config[CONF_ZONES]
    zones = [
        Zone(
            zc[CONF_ZONE_ID],
            priority=zc[CONF_PRIORITY],
            ready_timeout=zc[CONF_READY_TIMEOUT],
        )
        for zc in zone_configs
    ]
    sequencer = Sequencer(
        zones=zones,
        min_cycle_duration=config[CONF_MIN_CYCLE_DURATION],
        initial=Mode.COOL,  # overwritten by SharedModeSensor's restore, if any
        default_mode=config.get(CONF_DEFAULT_MODE),
    )
    coordinator = SharedModeCoordinator(hass, sequencer, zones, zone_configs)
    await coordinator.async_setup_listeners()
    # Starts the periodic poll too -- update_interval does nothing until
    # a first refresh has run.
    await coordinator.async_refresh()
    async_add_entities([SharedModeSensor(coordinator)])


class SharedModeCoordinator(DataUpdateCoordinator[Mode]):
    """Owns the Sequencer and every Zone."""

    def __init__(
        self,
        hass: HomeAssistant,
        sequencer: Sequencer,
        zones: list[Zone],
        zone_configs: list[dict[str, Any]],
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="Climate Zone Sequencer",
            update_interval=POLL_INTERVAL,
        )
        self.sequencer = sequencer
        self._zones = zones
        self._zone_configs = zone_configs
        self._unsubs: list[CALLBACK_TYPE] = []

    async def async_setup_listeners(self) -> None:
        for zone, zc in zip(self._zones, self._zone_configs, strict=True):
            self._track(zc[CONF_DEMAND_HEAT_ENTITY], self._make_demand_handler(zone, Mode.HEAT))
            self._track(zc[CONF_DEMAND_COOL_ENTITY], self._make_demand_handler(zone, Mode.COOL))
            self._track(zc[CONF_LIVENESS_ENTITY], self._make_liveness_handler(zone))

    def _track(self, entity_id: str, handler) -> None:
        self._unsubs.append(async_track_state_change_event(self.hass, entity_id, handler))

    def _make_demand_handler(self, zone: Zone, mode: Mode):
        async def _handle(event: Event[EventStateChangedData]) -> None:
            new_state = event.data["new_state"]
            calling = new_state is not None and new_state.state == STATE_ON
            zone.record(mode if calling else None, now=self.hass.loop.time())
            await self.async_request_refresh()

        return _handle

    def _make_liveness_handler(self, zone: Zone):
        async def _handle(event: Event[EventStateChangedData]) -> None:
            # Confirms liveness; doesn't change the zone's existing want.
            zone.record(zone.mode, now=self.hass.loop.time())
            await self.async_request_refresh()

        return _handle

    async def _async_update_data(self) -> Mode:
        return self.sequencer.evaluate(now=self.hass.loop.time())


class SharedModeSensor(CoordinatorEntity[SharedModeCoordinator], SensorEntity, RestoreEntity):
    """The current shared Mode (heat|cool), persisted across restarts."""

    _attr_name = "Shared Mode"
    _attr_icon = "mdi:scale-balance"

    def __init__(self, coordinator: SharedModeCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = "shared_mode"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in (Mode.HEAT, Mode.COOL):
            self.coordinator.sequencer.restore(Mode(last.state))

    @property
    def native_value(self) -> str:
        return self.coordinator.sequencer.mode.value
