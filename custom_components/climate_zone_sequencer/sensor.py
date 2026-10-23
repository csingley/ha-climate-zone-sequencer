"""HA platform entity for climate-zone-sequencer.

YAML/PLATFORM_SCHEMA, no config_flow. Every refresh observes each zone as a
whole -- a snapshot of all its entities from hass.states -- then evaluates;
startup, entity changes and the poll interval all go through that one path.
A zone whose demand entities are unavailable is lost (HA's integration does
the liveness probing). Only the Sequencer's last Mode is restored.
"""

from __future__ import annotations

import dataclasses
import logging
import math
from datetime import timedelta
from typing import Any, cast

import voluptuous as vol
from homeassistant.components.sensor import PLATFORM_SCHEMA, SensorEntity
from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.debounce import Debouncer
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
CONF_NAME = "name"
CONF_DEMAND_HEAT = "demand_heat"
CONF_DEMAND_COOL = "demand_cool"
CONF_URGENCY_TIER = "urgency_tier"
CONF_COMFORT_PRIORITY = "comfort_priority"
CONF_MIN_CYCLE_DURATION = "min_cycle_duration"
CONF_DEFAULT_MODE = "default_mode"
CONF_LAST_KNOWN_TTL = "last_known_ttl"

# Also the heartbeat: consumers that treat a quiet sensor as dead (e.g. a
# node's staleness check) need it re-published even when the Mode is unchanged.
POLL_INTERVAL = timedelta(seconds=30)
DEFAULT_LAST_KNOWN_TTL = timedelta(minutes=30)
UNSEEN = {STATE_UNAVAILABLE, STATE_UNKNOWN}

ZONE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Required(CONF_DEMAND_HEAT): cv.entity_id,
        vol.Required(CONF_DEMAND_COOL): cv.entity_id,
        vol.Optional(CONF_URGENCY_TIER): cv.entity_id,
        vol.Optional(CONF_COMFORT_PRIORITY): cv.entity_id,
    }
)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_ZONES): vol.All(cv.ensure_list, [ZONE_SCHEMA]),
        vol.Required(CONF_MIN_CYCLE_DURATION): cv.time_period,
        vol.Optional(CONF_DEFAULT_MODE): vol.Coerce(Mode),
        vol.Optional(CONF_LAST_KNOWN_TTL, default=DEFAULT_LAST_KNOWN_TTL): cv.time_period,
    }
)


@dataclasses.dataclass(frozen=True)
class ZoneEntities:
    """Where one zone's facts live in HA. Rank is (urgency_tier, comfort_priority)."""

    name: str
    demand_heat: str
    demand_cool: str
    urgency_tier: str | None
    comfort_priority: str | None

    @property
    def entity_ids(self) -> list[str]:
        ids = [self.demand_heat, self.demand_cool, self.urgency_tier, self.comfort_priority]
        return [eid for eid in ids if eid is not None]


async def async_setup_platform(
    hass: HomeAssistant,
    config: dict[str, Any],
    async_add_entities: AddEntitiesCallback,
    discovery_info: dict[str, Any] | None = None,
) -> None:
    # Config order is the zone id, so it's the tie-break between equal ranks.
    entities = [
        ZoneEntities(
            name=zc[CONF_NAME],
            demand_heat=zc[CONF_DEMAND_HEAT],
            demand_cool=zc[CONF_DEMAND_COOL],
            urgency_tier=zc.get(CONF_URGENCY_TIER),
            comfort_priority=zc.get(CONF_COMFORT_PRIORITY),
        )
        for zc in config[CONF_ZONES]
    ]
    zones = [Zone(i, last_known_ttl=config[CONF_LAST_KNOWN_TTL]) for i in range(len(entities))]
    sequencer = Sequencer(
        zones=zones,
        min_cycle_duration=config[CONF_MIN_CYCLE_DURATION],
        initial=Mode.COOL,  # overwritten by SharedModeSensor's restore, if any
        default_mode=config.get(CONF_DEFAULT_MODE),
    )
    coordinator = SharedModeCoordinator(hass, sequencer, list(zip(zones, entities, strict=True)))
    coordinator.async_watch()
    # Starts the poll too -- update_interval does nothing until a first refresh.
    await coordinator.async_refresh()
    async_add_entities([SharedModeSensor(coordinator)])


class SharedModeCoordinator(DataUpdateCoordinator[Mode]):
    """Owns the Sequencer and every Zone, and observes the Zones from HA."""

    def __init__(
        self,
        hass: HomeAssistant,
        sequencer: Sequencer,
        zones: list[tuple[Zone, ZoneEntities]],
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="Climate Zone Sequencer",
            update_interval=POLL_INTERVAL,
            # React to a demand change within a second, not the 10s default.
            request_refresh_debouncer=Debouncer(hass, _LOGGER, cooldown=1.0, immediate=True),
        )
        self.sequencer = sequencer
        self.zones = zones
        self._unwatch: CALLBACK_TYPE | None = None

    def async_watch(self) -> None:
        entity_ids = [eid for _, entities in self.zones for eid in entities.entity_ids]

        async def _changed(event: Event[EventStateChangedData]) -> None:
            await self.async_request_refresh()

        self._unwatch = async_track_state_change_event(self.hass, entity_ids, _changed)

    def now(self) -> float:
        return self.hass.loop.time()

    async def _async_update_data(self) -> Mode:
        now = self.now()
        for zone, entities in self.zones:
            self._observe(zone, entities, now)
        return self.sequencer.evaluate(now)

    def _observe(self, zone: Zone, entities: ZoneEntities, now: float) -> None:
        heat = self.hass.states.get(entities.demand_heat)
        cool = self.hass.states.get(entities.demand_cool)
        if heat is None or cool is None or {heat.state, cool.state} & UNSEEN:
            zone.lost(now)
            return
        heating, cooling = heat.state == STATE_ON, cool.state == STATE_ON
        if heating and cooling:
            _LOGGER.warning("%s is calling both heat and cool; ignoring its want", entities.name)
        want = None if heating == cooling else Mode.HEAT if heating else Mode.COOL
        zone.observe(want, (self._rank(entities.urgency_tier), self._rank(entities.comfort_priority)))

    def _rank(self, entity_id: str | None) -> float:
        if entity_id is None:
            return 0.0  # not configured: no say in the ordering
        state = self.hass.states.get(entity_id)
        try:
            return float(state.state) if state is not None else math.inf
        except ValueError:
            # Unreadable sorts last: never outranks a zone whose rank is known.
            return math.inf


class SharedModeSensor(CoordinatorEntity[SharedModeCoordinator], SensorEntity, RestoreEntity):
    """The current shared Mode (heat|cool), persisted across restarts."""

    _attr_name = "Shared Mode"
    _attr_icon = "mdi:scale-balance"
    # Write every refresh even when unchanged: see POLL_INTERVAL.
    _attr_force_update = True

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

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        now = self.coordinator.now()
        sequencer = self.coordinator.sequencer
        since = sequencer.seconds_since_change(now)
        return {
            "zones": [_zone_attributes(zone, entities, now) for zone, entities in self.coordinator.zones],
            "seconds_since_mode_change": None if since is None else round(since),
            "mode_change_allowed": sequencer.change_allowed(now),
        }


def _zone_attributes(zone: Zone, entities: ZoneEntities, now: float) -> dict[str, Any]:
    vote = zone.vote(now)
    want = vote.want.value if vote is not None and vote.want is not None else None
    # This module builds rank as (urgency_tier, comfort_priority). Attributes
    # are serialized as JSON, which has no infinity.
    ranks = cast(tuple[float, float], vote.rank) if vote is not None else None
    rank = [None if math.isinf(r) else r for r in ranks] if ranks is not None else None
    return {"name": entities.name, "status": zone.status(now).value, "want": want, "rank": rank}
