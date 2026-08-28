"""Pure computation of candidate_stages due dates from the journey_stages template.

No database access, no I/O, no datetime.now() — every input is passed in, so the
result is fully determined by (offer_date, joining_date, stages) and reproducible.

Offer-anchored stages (offer_accepted, welcome, documentation, manager_intro,
team_context) are scheduled relative to offer_date. Joining-anchored stages
(relocation_check, pre_joining_checkin, joining) are scheduled relative to
joining_date, which is a hard commitment and is never moved.

On a short notice period the two anchors collide: an offer-anchored stage's raw
due date can land on or after a joining-anchored stage's raw due date, even
though it comes earlier in the sequence. Rather than let dates run backwards,
the offer-anchored stages are compressed proportionally (their relative spacing
is scaled down, preserving order) into the window between offer_date and the
earliest joining-anchored due date. If that window itself has zero or negative
width (a very short notice period pushes a joining-anchored stage's raw date
before offer_date), the offer-anchored stages all collapse to offer_date and the
joining-anchored stages are pushed forward just enough to stay non-decreasing.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Protocol

from app.enums import StageAnchor


class StageTemplate(Protocol):
    """Shape required of each stage: a journey_stages row, or any stand-in with
    these four attributes (used directly by tests, without touching the DB)."""

    key: str
    anchor: StageAnchor
    offset_days: int
    sequence_order: int


def compute_stage_schedule(
    offer_date: date,
    joining_date: date,
    stages: list[StageTemplate],
) -> list[tuple[str, date]]:
    """Return [(stage_key, due_date), ...] in sequence_order, guaranteed to be
    monotonically non-decreasing by due_date."""

    ordered = sorted(stages, key=lambda s: s.sequence_order)
    offer_stages = [s for s in ordered if s.anchor == StageAnchor.OFFER]
    joining_stages = [s for s in ordered if s.anchor == StageAnchor.JOINING]

    raw_offer = [offer_date + timedelta(days=s.offset_days) for s in offer_stages]
    raw_join = [joining_date + timedelta(days=s.offset_days) for s in joining_stages]

    window_start = offer_date
    earliest_join = min(raw_join) if raw_join else joining_date
    window_end = max(window_start, min(earliest_join, joining_date))

    compressed_offer = _compress_into_window(raw_offer, window_start, window_end)

    prev = compressed_offer[-1] if compressed_offer else window_start
    resolved_join: list[date] = []
    for due in raw_join:
        due = max(due, prev)
        resolved_join.append(due)
        prev = due

    resolved: dict[str, date] = {
        s.key: d for s, d in zip(offer_stages, compressed_offer)
    }
    resolved.update({s.key: d for s, d in zip(joining_stages, resolved_join)})

    return [(s.key, resolved[s.key]) for s in ordered]


def _compress_into_window(
    raw_dates: list[date],
    window_start: date,
    window_end: date,
) -> list[date]:
    """Scale raw_dates (assumed non-decreasing, all >= window_start under normal
    conditions) so the whole set fits within [window_start, window_end],
    preserving relative order. Never expands — only compresses when the raw
    span exceeds the window."""

    if not raw_dates:
        return []

    original_span = (raw_dates[-1] - window_start).days
    target_span = (window_end - window_start).days
    scale = 1.0 if original_span <= 0 else min(1.0, target_span / original_span)

    result: list[date] = []
    prev: date | None = None
    for due in raw_dates:
        offset_from_start = (due - window_start).days
        candidate = window_start + timedelta(days=round(offset_from_start * scale))
        if candidate > window_end:
            candidate = window_end
        if prev is not None and candidate < prev:
            candidate = prev
        result.append(candidate)
        prev = candidate
    return result
