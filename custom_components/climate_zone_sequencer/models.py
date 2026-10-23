"""Core domain types and decision logic for climate-zone-sequencer.

Zones are seen only through observations, and each derives its current
Vote from the latest one. Sequencer picks one shared Mode from the votes
of a fixed collection of Zones, debounced against short-cycling.
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


@enum.unique
class Status(str, enum.Enum):
    """How far a Zone's vote can be trusted right now."""

    LIVE = "live"
    STALE = "stale"  # out of sight, last-known vote still within last_known_ttl
    EXPIRED = "expired"  # out of sight for longer than last_known_ttl
    UNKNOWN = "unknown"  # never seen


@dataclasses.dataclass(frozen=True)
class Vote:
    """What a Zone wants, and how it ranks against the others.

    Attributes:
        want: None for no active want.
        rank: Opaque; lower sorts first. Meaning is up to the caller.
    """

    want: Mode | None
    rank: Orderable


class Zone:
    """One zone, seen only through observations.

    Out of sight, a Zone keeps its last-known vote for last_known_ttl: it
    may still be running that want, and can't be influenced either way, so
    dropping it would let a sibling contend blind. Past the TTL it lapses.

    Args:
        id: This zone's identity; also the tie-break between equal ranks.
        last_known_ttl: How long a lost zone's last-known vote still counts.
    """

    def __init__(self, id: int, *, last_known_ttl: timedelta) -> None:
        self.id = id
        self.last_known_ttl = last_known_ttl.total_seconds()
        self._vote: Vote | None = None
        self._lost_since: float | None = None

    def observe(self, want: Mode | None, rank: Orderable) -> None:
        """Record what the zone wants, seen just now."""
        self._vote = Vote(want, rank)
        self._lost_since = None

    def lost(self, now: float) -> None:
        """Record that the zone can't be seen; the TTL runs from the first call."""
        if self._lost_since is None:
            self._lost_since = now

    def status(self, now: float) -> Status:
        if self._vote is None:
            return Status.UNKNOWN
        if self._lost_since is None:
            return Status.LIVE
        if now - self._lost_since <= self.last_known_ttl:
            return Status.STALE
        return Status.EXPIRED

    def vote(self, now: float) -> Vote | None:
        """The vote that counts right now, if any."""
        return self._vote if self.status(now) in (Status.LIVE, Status.STALE) else None


class Sequencer:
    """Picks one shared Mode from a fixed collection of Zones' votes,
    debounced against short-cycling.

    Args:
        zones: Fixed roster for the lifetime of this Sequencer.
        min_cycle_duration: Minimum time between Mode flips.
        initial: Mode to start in.
        default_mode: Mode to fall back to when no zone has a want.
                      Defaults to holding the current Mode.
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

    def change_allowed(self, now: float) -> bool:
        return self._last_change is None or now - self._last_change >= self.min_cycle_duration

    def seconds_since_change(self, now: float) -> float | None:
        return None if self._last_change is None else now - self._last_change

    def evaluate(self, now: float) -> Mode:
        """Recompute and return the current Mode from the Zones' votes."""
        contenders: list[tuple[Orderable, int, Mode]] = []
        for zone in self.zones.values():
            vote = zone.vote(now)
            if vote is not None and vote.want is not None:
                contenders.append((vote.rank, zone.id, vote.want))
        wanted = [want for _, _, want in sorted(contenders, key=lambda c: (c[0], c[1]))]

        match list(set(wanted)):
            case []:
                proposed = self.default_mode if self.default_mode is not None else self._mode
            case [mode]:
                proposed = mode
            case _:
                proposed = wanted[0]  # contention: highest rank wins

        if proposed != self._mode and self.change_allowed(now):
            self._mode = proposed
            self._last_change = now

        return self._mode
