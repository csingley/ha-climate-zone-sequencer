"""Unit tests for Sequencer (models.py).

No Home Assistant dependency -- plain pytest.
"""

import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from custom_components.climate_zone_sequencer.models import Mode, Sequencer, Zone  # noqa: E402


def _zone(zone_id: int, priority: int = 0, timeout_seconds: float = 60) -> Zone:
    return Zone(zone_id, priority=priority, ready_timeout=timedelta(seconds=timeout_seconds))


def test_holds_at_initial_mode_until_every_zone_has_reported_once():
    z1, z2 = _zone(1), _zone(2)
    s = Sequencer(zones=[z1, z2], min_cycle_duration=timedelta(seconds=0), initial=Mode.COOL)
    assert s.evaluate(now=0) == Mode.COOL

    z1.record(Mode.HEAT, now=0)
    # z2 has still never reported -- must keep holding, not assume idle.
    assert s.evaluate(now=0) == Mode.COOL


def test_flips_to_sole_demand_once_every_zone_is_ready():
    z1, z2 = _zone(1), _zone(2)
    s = Sequencer(zones=[z1, z2], min_cycle_duration=timedelta(seconds=0), initial=Mode.COOL)
    z1.record(Mode.HEAT, now=0)
    z2.record(None, now=0)
    assert s.evaluate(now=0) == Mode.HEAT


def test_holds_when_a_previously_ready_zone_goes_stale():
    z1, z2 = _zone(1, timeout_seconds=60), _zone(2, timeout_seconds=60)
    s = Sequencer(zones=[z1, z2], min_cycle_duration=timedelta(seconds=0), initial=Mode.COOL)
    z1.record(Mode.HEAT, now=0)
    z2.record(None, now=0)
    assert s.evaluate(now=0) == Mode.HEAT

    # z2 never reports again; past its timeout it must force a hold, even
    # though z1 (still ready) would otherwise keep calling for heat.
    assert s.evaluate(now=1000) == Mode.HEAT  # holding at HEAT, not re-deciding
    z1.record(Mode.COOL, now=1000)  # z1 changes its mind
    assert s.evaluate(now=1000) == Mode.HEAT  # still held: z2 is stale


def test_contention_resolves_by_priority_lower_wins():
    z1, z2 = _zone(1, priority=5), _zone(2, priority=0)
    s = Sequencer(zones=[z1, z2], min_cycle_duration=timedelta(seconds=0), initial=Mode.COOL)
    z1.record(Mode.HEAT, now=0)
    z2.record(Mode.COOL, now=0)
    assert s.evaluate(now=0) == Mode.COOL


def test_zone_id_breaks_ties_when_priority_is_equal():
    z1, z2 = _zone(1, priority=0), _zone(2, priority=0)
    s = Sequencer(zones=[z1, z2], min_cycle_duration=timedelta(seconds=0), initial=Mode.COOL)
    z1.record(Mode.HEAT, now=0)
    z2.record(Mode.COOL, now=0)
    assert s.evaluate(now=0) == Mode.HEAT  # zone 1 < zone 2


def test_default_mode_used_when_no_zone_has_an_active_want():
    z1, z2 = _zone(1), _zone(2)
    s = Sequencer(
        zones=[z1, z2],
        min_cycle_duration=timedelta(seconds=0),
        initial=Mode.COOL,
        default_mode=Mode.HEAT,
    )
    z1.record(None, now=0)
    z2.record(None, now=0)
    assert s.evaluate(now=0) == Mode.HEAT


def test_holds_current_mode_as_default_when_default_mode_not_set():
    z1 = _zone(1)
    s = Sequencer(zones=[z1], min_cycle_duration=timedelta(seconds=0), initial=Mode.COOL)
    z1.record(None, now=0)
    assert s.evaluate(now=0) == Mode.COOL


def test_min_cycle_duration_blocks_a_too_soon_flip():
    z1 = _zone(1)
    s = Sequencer(zones=[z1], min_cycle_duration=timedelta(seconds=600), initial=Mode.COOL)
    z1.record(Mode.HEAT, now=0)
    assert s.evaluate(now=0) == Mode.HEAT  # first-ever decision always allowed

    z1.record(Mode.COOL, now=10)
    assert s.evaluate(now=10) == Mode.HEAT  # too soon, held

    z1.record(Mode.COOL, now=700)
    assert s.evaluate(now=700) == Mode.COOL  # dwell elapsed
