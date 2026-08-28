from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pytest

from app.enums import StageAnchor
from app.services.stage_scheduler import compute_stage_schedule


@dataclass(frozen=True)
class Stage:
    key: str
    anchor: StageAnchor
    offset_days: int
    sequence_order: int


# Exactly the seeded journey_stages template from CLAUDE.md.
JOURNEY_STAGES: list[Stage] = [
    Stage("offer_accepted", StageAnchor.OFFER, 0, 1),
    Stage("welcome", StageAnchor.OFFER, 1, 2),
    Stage("documentation", StageAnchor.OFFER, 3, 3),
    Stage("manager_intro", StageAnchor.OFFER, 21, 4),
    Stage("team_context", StageAnchor.OFFER, 35, 5),
    Stage("relocation_check", StageAnchor.JOINING, -25, 6),
    Stage("pre_joining_checkin", StageAnchor.JOINING, -10, 7),
    Stage("joining", StageAnchor.JOINING, 0, 8),
]

STAGE_KEYS_IN_ORDER = [s.key for s in JOURNEY_STAGES]

OFFER_DATE = date(2026, 1, 1)


def _joining(notice_days: int) -> date:
    return OFFER_DATE + timedelta(days=notice_days)


def _assert_monotonic_non_decreasing(schedule: list[tuple[str, date]]) -> None:
    dates = [d for _, d in schedule]
    for previous, current in zip(dates, dates[1:]):
        assert current >= previous, f"schedule went backwards: {schedule}"


def _assert_order_preserved(schedule: list[tuple[str, date]]) -> None:
    assert [key for key, _ in schedule] == STAGE_KEYS_IN_ORDER


def test_90_day_notice_no_compression():
    joining_date = _joining(90)
    schedule = compute_stage_schedule(OFFER_DATE, joining_date, JOURNEY_STAGES)
    result = dict(schedule)

    expected = {
        "offer_accepted": OFFER_DATE,
        "welcome": OFFER_DATE + timedelta(days=1),
        "documentation": OFFER_DATE + timedelta(days=3),
        "manager_intro": OFFER_DATE + timedelta(days=21),
        "team_context": OFFER_DATE + timedelta(days=35),
        "relocation_check": joining_date - timedelta(days=25),
        "pre_joining_checkin": joining_date - timedelta(days=10),
        "joining": joining_date,
    }
    assert result == expected
    _assert_order_preserved(schedule)
    _assert_monotonic_non_decreasing(schedule)


def test_60_day_notice_boundary_no_compression():
    """60 days is the exact crossover: offer+35 == joining-25. Touching but not
    backwards, so no compression should be triggered."""
    joining_date = _joining(60)
    schedule = compute_stage_schedule(OFFER_DATE, joining_date, JOURNEY_STAGES)
    result = dict(schedule)

    expected = {
        "offer_accepted": OFFER_DATE,
        "welcome": OFFER_DATE + timedelta(days=1),
        "documentation": OFFER_DATE + timedelta(days=3),
        "manager_intro": OFFER_DATE + timedelta(days=21),
        "team_context": OFFER_DATE + timedelta(days=35),
        "relocation_check": joining_date - timedelta(days=25),
        "pre_joining_checkin": joining_date - timedelta(days=10),
        "joining": joining_date,
    }
    assert result == expected
    assert result["team_context"] == result["relocation_check"]
    _assert_order_preserved(schedule)
    _assert_monotonic_non_decreasing(schedule)


def test_30_day_notice_triggers_compression():
    """30-day notice puts team_context (offer+35) after relocation_check
    (joining-25) unless the offer-anchored stages are compressed."""
    joining_date = _joining(30)
    schedule = compute_stage_schedule(OFFER_DATE, joining_date, JOURNEY_STAGES)
    result = dict(schedule)

    # Window for offer-anchored stages is [offer_date, offer_date+5] (relocation_check
    # lands at joining-25 = offer+5), so the 0..35 day spread scales by 1/7.
    expected = {
        "offer_accepted": OFFER_DATE,
        "welcome": OFFER_DATE,
        "documentation": OFFER_DATE,
        "manager_intro": OFFER_DATE + timedelta(days=3),
        "team_context": OFFER_DATE + timedelta(days=5),
        "relocation_check": OFFER_DATE + timedelta(days=5),
        "pre_joining_checkin": OFFER_DATE + timedelta(days=20),
        "joining": OFFER_DATE + timedelta(days=30),
    }
    assert result == expected

    # Compression actually happened: team_context moved earlier than its raw offer+35.
    assert result["team_context"] < OFFER_DATE + timedelta(days=35)
    assert result["team_context"] <= result["relocation_check"]
    _assert_order_preserved(schedule)
    _assert_monotonic_non_decreasing(schedule)


def test_15_day_notice_extreme_compression():
    """15-day notice is short enough that relocation_check's raw date
    (joining-25) falls before offer_date itself. All offer-anchored stages
    collapse to offer_date; joining-anchored stages get pushed forward just
    enough to stay non-decreasing."""
    joining_date = _joining(15)
    schedule = compute_stage_schedule(OFFER_DATE, joining_date, JOURNEY_STAGES)
    result = dict(schedule)

    expected = {
        "offer_accepted": OFFER_DATE,
        "welcome": OFFER_DATE,
        "documentation": OFFER_DATE,
        "manager_intro": OFFER_DATE,
        "team_context": OFFER_DATE,
        "relocation_check": OFFER_DATE,
        "pre_joining_checkin": OFFER_DATE + timedelta(days=5),
        "joining": OFFER_DATE + timedelta(days=15),
    }
    assert result == expected
    assert result["joining"] == joining_date
    _assert_order_preserved(schedule)
    _assert_monotonic_non_decreasing(schedule)


@pytest.mark.parametrize("notice_days", [15, 21, 30, 45, 60, 75, 90, 120])
def test_monotonic_non_decreasing_across_notice_periods(notice_days: int):
    joining_date = _joining(notice_days)
    schedule = compute_stage_schedule(OFFER_DATE, joining_date, JOURNEY_STAGES)

    _assert_order_preserved(schedule)
    _assert_monotonic_non_decreasing(schedule)

    result = dict(schedule)
    assert result["offer_accepted"] == OFFER_DATE
    assert result["joining"] == joining_date
    # Nothing should ever be scheduled outside [offer_date, joining_date].
    for _, due in schedule:
        assert OFFER_DATE <= due <= joining_date


def test_schedule_length_and_keys_match_input():
    joining_date = _joining(45)
    schedule = compute_stage_schedule(OFFER_DATE, joining_date, JOURNEY_STAGES)
    assert len(schedule) == len(JOURNEY_STAGES)
    assert {key for key, _ in schedule} == {s.key for s in JOURNEY_STAGES}
