"""APScheduler wiring for the nightly engagement sweep.

In-process and deliberately simple: one BackgroundScheduler, one cron job,
started and stopped by the FastAPI lifespan. There is no job store, so a
missed run is missed rather than replayed — which is correct for a sweep that
recomputes everything from current state each time. Nothing accumulates while
the process is down; tomorrow's run sees the same candidates.

RUN_SCHEDULER exists because a background thread firing real sweeps during a
test run would create actions the tests never asked for and burn provider
quota. It defaults to true so `docker compose up` behaves like production.

At a million candidates this is the first thing to go: a single in-process
thread doing per-candidate AI calls does not scale, and the sweep becomes a
batched job feeding a work queue. Documented in the README's scaling section.
"""

from __future__ import annotations

import logging
from datetime import date

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.db import SessionLocal
from app.services import automation_service, risk_service

logger = logging.getLogger(__name__)

SWEEP_JOB_ID = "nightly_engagement_sweep"


def run_nightly_sweep() -> None:
    """The job body: rescore every pending candidate, then run both automation
    rules. Runs on the scheduler's thread, so it opens its own session — the
    request-scoped one from get_db does not exist here.

    The recompute has to come first, and it is not optional. Risk is otherwise
    only recalculated when an interaction is written, and a candidate who has
    gone silent by definition generates no interactions — so the badge for
    exactly the candidate this product exists to catch is the one that goes
    stale. Measured against the seeded data, 11 of 40 pending candidates sit
    below their true rule floor after three days without this pass.

    Ordering matters for a second reason: the sweep's own decisions read
    candidate.risk_level, so scoring first means the actions it files carry
    today's risk rather than whatever was last written.
    """
    logger.info("Nightly maintenance triggered by the scheduler")
    with SessionLocal() as db:
        try:
            risk = risk_service.recompute_all(db, date.today(), actor="scheduler")
            logger.info(
                "Nightly risk recompute: %d scanned, %d level changes, distribution %s",
                risk["scanned"],
                risk["level_changed"],
                risk["distribution"],
            )
        except Exception:
            # Do not abandon the sweep because scoring failed. Stale badges are
            # bad; a silent candidate with no action filed at all is worse.
            logger.exception("Nightly risk recompute failed; continuing to the sweep")

        try:
            automation_service.run_engagement_sweep(db, actor="scheduler")
        except Exception:
            # A scheduled job that raises would otherwise vanish into
            # APScheduler's own logger with no context.
            logger.exception("Nightly engagement sweep failed")


def build_scheduler() -> BackgroundScheduler:
    """Construct the scheduler without starting it, so the job wiring is
    testable without a thread running real sweeps."""
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        run_nightly_sweep,
        CronTrigger(hour=settings.sweep_hour, minute=settings.sweep_minute, timezone="UTC"),
        id=SWEEP_JOB_ID,
        name="Nightly risk recompute + engagement sweep",
        replace_existing=True,
        # A sweep that overruns must not have a second copy started on top of
        # it: two concurrent sweeps would race the idempotency check.
        max_instances=1,
        # After downtime, run once — not once for every missed night.
        coalesce=True,
        misfire_grace_time=3600,
    )
    return scheduler


def start_scheduler() -> BackgroundScheduler | None:
    if not settings.run_scheduler:
        logger.info("RUN_SCHEDULER is false — the nightly sweep is not scheduled")
        return None

    scheduler = build_scheduler()
    scheduler.start()
    logger.info(
        "Nightly engagement sweep scheduled for %02d:%02d UTC daily",
        settings.sweep_hour,
        settings.sweep_minute,
    )
    return scheduler
