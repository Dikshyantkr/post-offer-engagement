"""Pure unit tests for the rule-based risk bands.

compute_base_risk takes no database and no clock, so every band boundary is
tested from both sides with plain integers.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

from app.enums import BlockerCategory, BlockerSignal, RecruiterRead, RiskLevel
from app.services.risk_service import BAND_RANGES, compute_base_risk, resolve_blocker_signal

# Inputs that are individually harmless, used to isolate one rule at a time.
QUIET = 0  # days_since_contact
FAR = 60  # days_to_joining
NO_LAG = 0  # max_stage_overdue_days
NO_BLOCKER = BlockerSignal.NONE


def band(
    days_to_joining=FAR,
    days_since_contact=QUIET,
    max_stage_overdue_days=NO_LAG,
    blocker_signal=NO_BLOCKER,
):
    return compute_base_risk(
        days_to_joining, days_since_contact, max_stage_overdue_days, blocker_signal
    )[1]


def score(
    days_to_joining=FAR,
    days_since_contact=QUIET,
    max_stage_overdue_days=NO_LAG,
    blocker_signal=NO_BLOCKER,
):
    return compute_base_risk(
        days_to_joining, days_since_contact, max_stage_overdue_days, blocker_signal
    )[0]


# ---------------------------------------------------------------------------
# HIGH rule 1: silent >= 10 days
# ---------------------------------------------------------------------------


def test_silence_10_days_is_high_and_9_is_not():
    assert band(days_since_contact=10) == RiskLevel.HIGH
    assert band(days_since_contact=9) != RiskLevel.HIGH


# ---------------------------------------------------------------------------
# HIGH rule 2: silent >= 5 AND joining within 14
# ---------------------------------------------------------------------------


def test_silent_5_and_joining_within_14_is_high():
    assert band(days_since_contact=5, days_to_joining=14) == RiskLevel.HIGH


def test_both_sides_of_the_silent5_joining14_rule():
    # One day further out on either axis drops it out of HIGH.
    assert band(days_since_contact=5, days_to_joining=15) != RiskLevel.HIGH
    assert band(days_since_contact=4, days_to_joining=14) != RiskLevel.HIGH


# ---------------------------------------------------------------------------
# HIGH rule 3: any stage > 7 days overdue AND joining within 21
# ---------------------------------------------------------------------------


def test_stage_8_days_overdue_and_joining_within_21_is_high():
    assert band(max_stage_overdue_days=8, days_to_joining=21) == RiskLevel.HIGH


def test_both_sides_of_the_stage_lag_rule():
    # Exactly 7 days overdue is not "> 7".
    assert band(max_stage_overdue_days=7, days_to_joining=21) != RiskLevel.HIGH
    assert band(max_stage_overdue_days=8, days_to_joining=22) != RiskLevel.HIGH


# ---------------------------------------------------------------------------
# MEDIUM boundaries
# ---------------------------------------------------------------------------


def test_silence_7_days_is_medium_and_6_is_low():
    assert band(days_since_contact=7) == RiskLevel.MEDIUM
    assert band(days_since_contact=6) == RiskLevel.LOW


def test_joining_within_7_days_is_medium_regardless_of_contact():
    assert band(days_to_joining=7, days_since_contact=0) == RiskLevel.MEDIUM
    assert band(days_to_joining=8, days_since_contact=0) == RiskLevel.LOW


def test_any_stage_overdue_is_medium():
    assert band(max_stage_overdue_days=1) == RiskLevel.MEDIUM
    assert band(max_stage_overdue_days=0) == RiskLevel.LOW


# ---------------------------------------------------------------------------
# LOW
# ---------------------------------------------------------------------------


def test_healthy_candidate_is_low():
    assert band(days_to_joining=45, days_since_contact=2, max_stage_overdue_days=0) == RiskLevel.LOW


# ---------------------------------------------------------------------------
# Past joining date while still pending
# ---------------------------------------------------------------------------


def test_joining_date_already_passed_is_at_least_medium():
    # days_to_joining <= 7 covers negatives, so an overdue joiner never reads LOW.
    assert band(days_to_joining=-3, days_since_contact=0) == RiskLevel.MEDIUM
    assert band(days_to_joining=-3, days_since_contact=5) == RiskLevel.HIGH


# ---------------------------------------------------------------------------
# Blocker signal floors
# ---------------------------------------------------------------------------


def test_critical_blocker_forces_high_from_an_otherwise_calm_candidate():
    """Joining 60 days out, contacted today, nothing overdue — the
    time-based rules say LOW. A logged counter-offer overrides that."""
    assert band() == RiskLevel.LOW
    assert band(blocker_signal=BlockerSignal.CRITICAL) == RiskLevel.HIGH


def test_concern_blocker_forces_medium_from_an_otherwise_calm_candidate():
    assert band(blocker_signal=BlockerSignal.CONCERN) == RiskLevel.MEDIUM


def test_unsure_never_changes_the_band():
    for kwargs in (
        dict(),  # LOW
        dict(days_since_contact=7),  # MEDIUM
        dict(days_since_contact=10),  # HIGH
    ):
        without = band(**kwargs)
        with_unsure = band(**kwargs, blocker_signal=BlockerSignal.UNSURE)
        assert with_unsure == without, f"unsure moved the band for {kwargs}"


def test_unsure_raises_the_score_within_the_band():
    for kwargs in (dict(), dict(days_since_contact=7), dict(days_since_contact=10)):
        without = score(**kwargs)
        with_unsure = score(**kwargs, blocker_signal=BlockerSignal.UNSURE)
        assert with_unsure > without, f"unsure did not nudge the score for {kwargs}"


def test_blocker_floor_never_lowers_an_already_higher_band():
    """A silent-15-days candidate is HIGH on the time rules alone. A merely
    CONCERN-level blocker must not pull them down to MEDIUM."""
    assert band(days_since_contact=15) == RiskLevel.HIGH
    assert band(days_since_contact=15, blocker_signal=BlockerSignal.CONCERN) == RiskLevel.HIGH
    assert band(days_since_contact=15, blocker_signal=BlockerSignal.UNSURE) == RiskLevel.HIGH
    assert band(days_since_contact=15, blocker_signal=BlockerSignal.NONE) == RiskLevel.HIGH


def test_no_blocker_scores_exactly_as_before_the_signal_existed():
    """NONE must be a true no-op, so adding the fourth input cannot silently
    reprice every candidate who has no logged blocker."""
    explicit = compute_base_risk(30, 4, 2, BlockerSignal.NONE)
    omitted = compute_base_risk(30, 4, 2)
    assert explicit == omitted


@pytest.mark.parametrize(
    "signal,expected_floor",
    [
        (BlockerSignal.NONE, RiskLevel.LOW),
        (BlockerSignal.UNSURE, RiskLevel.LOW),
        (BlockerSignal.CONCERN, RiskLevel.MEDIUM),
        (BlockerSignal.CRITICAL, RiskLevel.HIGH),
    ],
)
def test_each_signal_produces_its_documented_floor(signal, expected_floor):
    assert band(blocker_signal=signal) == expected_floor


def test_more_severe_signal_never_lowers_the_score():
    previous = -1.0
    for signal in (
        BlockerSignal.NONE,
        BlockerSignal.UNSURE,
        BlockerSignal.CONCERN,
        BlockerSignal.CRITICAL,
    ):
        current = score(days_since_contact=12, blocker_signal=signal)
        assert current >= previous, f"score dropped at {signal}"
        previous = current


def test_blocker_score_still_lands_inside_its_band():
    for signal in BlockerSignal:
        value, level = compute_base_risk(60, 0, 0, signal)
        floor, ceiling = BAND_RANGES[level]
        assert floor <= value <= ceiling


# ---------------------------------------------------------------------------
# Score scale
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(days_since_contact=30, days_to_joining=1, max_stage_overdue_days=30),
        dict(days_since_contact=10),
        dict(days_since_contact=7),
        dict(days_to_joining=7),
        dict(max_stage_overdue_days=1),
        dict(days_to_joining=45, days_since_contact=0),
    ],
)
def test_score_always_lands_inside_its_own_band(kwargs):
    value, level = compute_base_risk(
        kwargs.get("days_to_joining", FAR),
        kwargs.get("days_since_contact", QUIET),
        kwargs.get("max_stage_overdue_days", NO_LAG),
    )
    floor, ceiling = BAND_RANGES[level]
    assert floor <= value <= ceiling, f"{value} outside {level} range {floor}-{ceiling}"


def test_score_bounds_are_never_exceeded():
    worst = score(days_to_joining=-100, days_since_contact=365, max_stage_overdue_days=365)
    best = score(days_to_joining=365, days_since_contact=0, max_stage_overdue_days=0)
    assert worst == 100.0
    assert 0.0 <= best <= 39.0


def test_more_silence_never_lowers_the_score():
    previous = -1.0
    for silent_days in range(0, 40):
        current = score(days_since_contact=silent_days, days_to_joining=30)
        assert current >= previous, f"score dropped at {silent_days} days of silence"
        previous = current


def test_closer_joining_never_lowers_the_score():
    previous = 101.0
    for days_out in range(60, -10, -1):
        current = score(days_to_joining=days_out, days_since_contact=3)
        assert current <= 100.0
        assert current >= 0.0
        if days_out < 60:
            assert current >= previous - 0.05, f"score dropped as joining approached at {days_out}"
        previous = current


def test_pure_function_is_deterministic():
    args = (12, 6, 3)
    assert compute_base_risk(*args) == compute_base_risk(*args)


# ---------------------------------------------------------------------------
# resolve_blocker_signal — reducing interactions to one signal
# ---------------------------------------------------------------------------


TODAY = date(2026, 6, 15)


class FakeInteraction:
    """Minimal stand-in: resolve_blocker_signal only touches these four
    attributes, so the resolver is testable without a database."""

    def __init__(self, days_ago, blocker_raised=False, blocker_category=None, recruiter_read=None):
        self.occurred_at = datetime.combine(
            TODAY - timedelta(days=days_ago), time(12, 0), tzinfo=timezone.utc
        )
        self.blocker_raised = blocker_raised
        self.blocker_category = blocker_category
        self.recruiter_read = recruiter_read


@pytest.mark.parametrize(
    "category", [BlockerCategory.COUNTER_OFFER, BlockerCategory.NOTICE_PERIOD]
)
def test_counter_offer_and_notice_period_resolve_to_critical(category):
    signal = resolve_blocker_signal(
        [FakeInteraction(2, blocker_raised=True, blocker_category=category)], TODAY
    )
    assert signal == BlockerSignal.CRITICAL


@pytest.mark.parametrize(
    "category",
    [
        BlockerCategory.RELOCATION,
        BlockerCategory.COMPENSATION,
        BlockerCategory.ROLE_SCOPE,
        BlockerCategory.PERSONAL,
    ],
)
def test_other_blocker_categories_resolve_to_concern(category):
    signal = resolve_blocker_signal(
        [FakeInteraction(2, blocker_raised=True, blocker_category=category)], TODAY
    )
    assert signal == BlockerSignal.CONCERN


def test_recruiter_read_worried_resolves_to_concern_without_a_blocker():
    signal = resolve_blocker_signal(
        [FakeInteraction(2, recruiter_read=RecruiterRead.WORRIED)], TODAY
    )
    assert signal == BlockerSignal.CONCERN


def test_recruiter_read_unsure_resolves_to_unsure():
    signal = resolve_blocker_signal(
        [FakeInteraction(2, recruiter_read=RecruiterRead.UNSURE)], TODAY
    )
    assert signal == BlockerSignal.UNSURE


def test_recruiter_read_on_track_resolves_to_none():
    signal = resolve_blocker_signal(
        [FakeInteraction(2, recruiter_read=RecruiterRead.ON_TRACK)], TODAY
    )
    assert signal == BlockerSignal.NONE


def test_most_severe_signal_wins_across_interactions():
    signal = resolve_blocker_signal(
        [
            FakeInteraction(1, recruiter_read=RecruiterRead.UNSURE),
            FakeInteraction(5, blocker_raised=True, blocker_category=BlockerCategory.COUNTER_OFFER),
            FakeInteraction(3, blocker_raised=True, blocker_category=BlockerCategory.RELOCATION),
        ],
        TODAY,
    )
    assert signal == BlockerSignal.CRITICAL


def test_signals_older_than_the_window_are_ignored():
    old = FakeInteraction(31, blocker_raised=True, blocker_category=BlockerCategory.COUNTER_OFFER)
    assert resolve_blocker_signal([old], TODAY) == BlockerSignal.NONE


def test_signal_exactly_at_the_window_edge_still_counts():
    edge = FakeInteraction(30, blocker_raised=True, blocker_category=BlockerCategory.COUNTER_OFFER)
    assert resolve_blocker_signal([edge], TODAY) == BlockerSignal.CRITICAL


def test_an_old_critical_does_not_mask_a_recent_lesser_signal():
    """The old counter-offer ages out; what's left is the recent unsure."""
    signal = resolve_blocker_signal(
        [
            FakeInteraction(
                60, blocker_raised=True, blocker_category=BlockerCategory.COUNTER_OFFER
            ),
            FakeInteraction(2, recruiter_read=RecruiterRead.UNSURE),
        ],
        TODAY,
    )
    assert signal == BlockerSignal.UNSURE


def test_no_interactions_resolves_to_none():
    assert resolve_blocker_signal([], TODAY) == BlockerSignal.NONE


def test_blocker_raised_without_a_category_still_counts_as_concern():
    signal = resolve_blocker_signal([FakeInteraction(1, blocker_raised=True)], TODAY)
    assert signal == BlockerSignal.CONCERN
