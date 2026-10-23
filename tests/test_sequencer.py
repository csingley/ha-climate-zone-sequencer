"""Unit tests for Sequencer (models.py). No Home Assistant dependency."""

import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from custom_components.climate_zone_sequencer.models import Mode, Sequencer, Zone  # noqa: E402


def _zone(zone_id: int, ttl_seconds: float = 60) -> Zone:
    return Zone(zone_id, last_known_ttl=timedelta(seconds=ttl_seconds))


def _sequencer(*zones: Zone, dwell: float = 0, **kwargs) -> Sequencer:
    return Sequencer(
        zones=zones, min_cycle_duration=timedelta(seconds=dwell), initial=Mode.COOL, **kwargs
    )


def test_an_unseen_zone_does_not_hold_the_others():
    z1, z2 = _zone(1), _zone(2)
    z1.observe(Mode.HEAT, rank=0)  # z2 never observed
    assert _sequencer(z1, z2).evaluate(now=0) is Mode.HEAT


def test_sole_want_wins():
    z1, z2 = _zone(1), _zone(2)
    z1.observe(Mode.HEAT, rank=0)
    z2.observe(None, rank=0)
    assert _sequencer(z1, z2).evaluate(now=0) is Mode.HEAT


def test_contention_resolves_by_rank_lower_wins():
    z1, z2 = _zone(1), _zone(2)
    z1.observe(Mode.HEAT, rank=(2, 50))
    z2.observe(Mode.COOL, rank=(0, 50))
    assert _sequencer(z1, z2).evaluate(now=0) is Mode.COOL


def test_zone_id_breaks_rank_ties():
    z1, z2 = _zone(1), _zone(2)
    z1.observe(Mode.HEAT, rank=(2, 50))
    z2.observe(Mode.COOL, rank=(2, 50))
    assert _sequencer(z1, z2).evaluate(now=0) is Mode.HEAT


def test_stale_zone_keeps_contending():
    z1, z2 = _zone(1, ttl_seconds=60), _zone(2)
    z1.observe(Mode.HEAT, rank=0)
    z2.observe(Mode.COOL, rank=1)
    z1.lost(now=0)
    assert _sequencer(z1, z2).evaluate(now=30) is Mode.HEAT


def test_expired_zone_no_longer_contends():
    z1, z2 = _zone(1, ttl_seconds=60), _zone(2)
    z1.observe(Mode.HEAT, rank=0)
    z2.observe(Mode.COOL, rank=1)
    z1.lost(now=0)
    assert _sequencer(z1, z2).evaluate(now=61) is Mode.COOL


def test_default_mode_when_no_zone_wants_anything():
    z1 = _zone(1)
    z1.observe(None, rank=0)
    assert _sequencer(z1, default_mode=Mode.HEAT).evaluate(now=0) is Mode.HEAT


def test_holds_current_mode_without_a_default():
    z1 = _zone(1)
    z1.observe(None, rank=0)
    assert _sequencer(z1).evaluate(now=0) is Mode.COOL


def test_min_cycle_duration_blocks_a_too_soon_flip():
    z1 = _zone(1)
    s = _sequencer(z1, dwell=600)
    z1.observe(Mode.HEAT, rank=0)
    assert s.evaluate(now=0) is Mode.HEAT  # first-ever change always allowed
    z1.observe(Mode.COOL, rank=0)
    assert s.evaluate(now=10) is Mode.HEAT
    assert not s.change_allowed(now=10)
    assert s.evaluate(now=600) is Mode.COOL


def test_seconds_since_change():
    z1 = _zone(1)
    s = _sequencer(z1)
    assert s.seconds_since_change(now=0) is None
    z1.observe(Mode.HEAT, rank=0)
    s.evaluate(now=5)
    assert s.seconds_since_change(now=12) == 7


def test_restore_is_not_a_flip():
    z1 = _zone(1)
    s = _sequencer(z1, dwell=600)
    s.restore(Mode.HEAT)
    assert s.mode is Mode.HEAT
    assert s.change_allowed(now=0)
