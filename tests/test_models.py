"""Unit tests for Zone (models.py). No Home Assistant dependency."""

import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from custom_components.climate_zone_sequencer.models import Mode, Status, Vote, Zone  # noqa: E402


def _zone(ttl_seconds: float = 60) -> Zone:
    return Zone(1, last_known_ttl=timedelta(seconds=ttl_seconds))


def test_unknown_until_first_observed():
    zone = _zone()
    assert zone.status(now=0) is Status.UNKNOWN
    assert zone.vote(now=0) is None


def test_observed_zone_is_live_with_its_vote():
    zone = _zone()
    zone.observe(Mode.HEAT, rank=(0, 50))
    assert zone.status(now=0) is Status.LIVE
    assert zone.vote(now=0) == Vote(Mode.HEAT, (0, 50))


def test_quiet_zone_stays_live_indefinitely():
    # Sources publish only on change; silence is not loss.
    zone = _zone(ttl_seconds=60)
    zone.observe(Mode.HEAT, rank=0)
    assert zone.status(now=10_000) is Status.LIVE


def test_no_want_is_still_a_live_vote():
    zone = _zone()
    zone.observe(None, rank=0)
    assert zone.status(now=0) is Status.LIVE
    assert zone.vote(now=0) == Vote(None, 0)


def test_lost_zone_keeps_last_known_vote_within_ttl():
    zone = _zone(ttl_seconds=60)
    zone.observe(Mode.COOL, rank=0)
    zone.lost(now=100)
    assert zone.status(now=160) is Status.STALE
    assert zone.vote(now=160) == Vote(Mode.COOL, 0)


def test_lost_zone_vote_lapses_past_ttl():
    zone = _zone(ttl_seconds=60)
    zone.observe(Mode.COOL, rank=0)
    zone.lost(now=100)
    assert zone.status(now=161) is Status.EXPIRED
    assert zone.vote(now=161) is None


def test_ttl_runs_from_first_loss_not_last_report():
    zone = _zone(ttl_seconds=60)
    zone.observe(Mode.COOL, rank=0)
    zone.lost(now=100)
    zone.lost(now=150)  # repeated "still lost" reports
    assert zone.status(now=161) is Status.EXPIRED


def test_ttl_ignores_how_long_the_zone_was_quiet_before_loss():
    zone = _zone(ttl_seconds=60)
    zone.observe(Mode.COOL, rank=0)
    zone.lost(now=10_000)
    assert zone.status(now=10_030) is Status.STALE


def test_observing_again_restores_live():
    zone = _zone(ttl_seconds=60)
    zone.observe(Mode.COOL, rank=0)
    zone.lost(now=100)
    zone.observe(Mode.HEAT, rank=0)
    assert zone.status(now=1000) is Status.LIVE
    assert zone.vote(now=1000) == Vote(Mode.HEAT, 0)


def test_lost_before_ever_seen_is_still_unknown():
    zone = _zone()
    zone.lost(now=0)
    assert zone.status(now=0) is Status.UNKNOWN
