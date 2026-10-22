"""Core domain types and decision logic for climate-zone-sequencer.

A Zone's demand and liveness both derive from its own observation
history, with no separately-mutated state. Sequencer arbitrates a
shared, mode-exclusive plant from a fixed collection of Zones.
"""

from __future__ import annotations

import dataclasses
import enum
from collections.abc import Collection
from datetime import timedelta
from typing import Any, Protocol


class Orderable(Protocol):
    """Supports <, usable as a sort key."""

    def __lt__(self, other: Any) -> bool: ...


@enum.unique
class Mode(str, enum.Enum):
    """One of the shared plant's mutually-exclusive operating modes."""

    HEAT = "heat"
    COOL = "cool"


@dataclasses.dataclass(frozen=True)
class ZoneEvent:
    """One confirmed-alive observation of a zone, at an instant.

    A failed probe produces no event -- staleness is derived from the
    gap since the latest one (Zone.ready()), never stored as an event.
    """

    timestamp: float
    mode: Mode | None


class Zone:
    """One zone's demand and liveness, derived from its own history.

    priority is config, not observation, so it's a plain mutable
    attribute rather than part of the event log. id also tie-breaks
    equal priorities.
    """

    def __init__(self, id: int, *, priority: Orderable, ready_timeout: timedelta) -> None:
        self.id = id
        self.priority = priority
        self.ready_timeout = ready_timeout.total_seconds()
        self._events: list[ZoneEvent] = []

    def record(self, mode: Mode | None, now: float) -> None:
        """Append a confirmed-alive observation (mode=None means idle)."""
        self._events.append(ZoneEvent(timestamp=now, mode=mode))

    @property
    def mode(self) -> Mode | None:
        """This zone's most recently observed want."""
        return self._events[-1].mode if self._events else None

    def ready(self, now: float) -> bool:
        """True if this zone has been observed within ready_timeout."""
        if not self._events:
            return False
        return now - self._events[-1].timestamp <= self.ready_timeout


class Sequencer:
    """Selects one shared Mode for a fixed collection of Zones, debounced
    against short-cycling.

    Holds the current Mode if any Zone isn't ready -- an untrusted Zone
    may be silently asserting a conflicting want we can't see.
    """

    def __init__(
        self,
        *,
        zones: Collection[Zone],
        min_cycle_duration: timedelta,
        initial: Mode,
        default_mode: Mode | None = None,
    ) -> None:
        self.zones = {zone.id: zone for zone in zones}
        self.min_cycle_duration = min_cycle_duration.total_seconds()
        self.default_mode = default_mode
        self._mode = initial
        self._last_change: float | None = None

    @property
    def mode(self) -> Mode:
        return self._mode

    def restore(self, mode: Mode) -> None:
        """Set the Mode directly, bypassing the min-cycle gate.

        For restoring state after a restart -- not a real flip.
        """
        self._mode = mode

    def evaluate(self, now: float) -> Mode:
        """Recompute and return the current Mode from the live Zones."""
        if not all(zone.ready(now) for zone in self.zones.values()):
            return self._mode

        active = (zone for zone in self.zones.values() if zone.mode is not None)
        wanted = [
            zone.mode for zone in sorted(active, key=lambda z: (z.priority, z.id))
        ]

        match list(set(wanted)):
            case []:
                proposed = self.default_mode if self.default_mode is not None else self._mode
            case [mode]:
                proposed = mode
            case _:
                proposed = wanted[0]  # contention: highest priority wins

        allowed = (
            self._last_change is None
            or now - self._last_change >= self.min_cycle_duration
        )
        if proposed != self._mode and allowed:
            self._mode = proposed
            self._last_change = now

        return self._mode
