"""Unit tests for Zone/ZoneEvent (models.py).

No Home Assistant dependency -- plain pytest.
"""

import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from custom_components.climate_zone_sequencer.models import Mode, Zone  # noqa: E402


def test_zone_starts_not_ready_and_with_no_mode():
    zone = Zone(1, priority=0, ready_timeout=timedelta(seconds=60))
    assert zone.mode is None
    assert zone.ready(now=0) is False


def test_zone_priority_is_set_at_construction_and_mutable():
    zone = Zone(1, priority=0, ready_timeout=timedelta(seconds=60))
    assert zone.priority == 0
    zone.priority = 5
    assert zone.priority == 5


def test_zone_record_updates_mode():
    zone = Zone(1, priority=0, ready_timeout=timedelta(seconds=60))
    zone.record(Mode.HEAT, now=0)
    assert zone.mode is Mode.HEAT


def test_zone_record_with_mode_none_is_confirmed_idle_not_unready():
    zone = Zone(1, priority=0, ready_timeout=timedelta(seconds=60))
    zone.record(None, now=0)
    assert zone.mode is None
    assert zone.ready(now=0) is True


def test_zone_ready_within_timeout():
    zone = Zone(1, priority=0, ready_timeout=timedelta(seconds=60))
    zone.record(Mode.HEAT, now=0)
    assert zone.ready(now=59) is True
    assert zone.ready(now=60) is True


def test_zone_not_ready_once_timeout_elapses():
    zone = Zone(1, priority=0, ready_timeout=timedelta(seconds=60))
    zone.record(Mode.HEAT, now=0)
    assert zone.ready(now=61) is False


def test_zone_mode_reflects_only_the_most_recent_event():
    zone = Zone(1, priority=0, ready_timeout=timedelta(seconds=60))
    zone.record(Mode.HEAT, now=0)
    zone.record(Mode.COOL, now=10)
    assert zone.mode is Mode.COOL


def test_zone_going_stale_then_recording_again_restores_readiness():
    zone = Zone(1, priority=0, ready_timeout=timedelta(seconds=60))
    zone.record(Mode.HEAT, now=0)
    assert zone.ready(now=100) is False
    zone.record(Mode.HEAT, now=100)
    assert zone.ready(now=100) is True
