import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import models  # noqa: F401  (registers models on Base.metadata)
from app.config import settings
from app.db import Base, engine
from app.errors import register_exception_handlers
from app.routers import (
    ai,
    analytics,
    automation,
    candidates,
    follow_up_actions,
    interactions,
    recruiters,
    risk,
    stages,
)
from app.scheduler import start_scheduler


# Uvicorn configures its own loggers and leaves the root logger at WARNING, so
# without this every logger.info() in the app is silently dropped. That matters
# beyond tidiness: Module 6's simulated send IS a log line — CLAUDE.md's
# "store generated_message, log it, do not send" — and a fallback that nobody
# can see in the logs is a fallback nobody knows happened.
logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    Base.metadata.create_all(bind=engine)

    # Returns None when RUN_SCHEDULER is false, which is how tests and CI keep
    # a background thread from firing real sweeps mid-run.
    scheduler = start_scheduler()
    try:
        yield
    finally:
        if scheduler is not None:
            # Do not block shutdown on a sweep that is mid-flight; it commits
            # per candidate, so whatever it finished is already durable.
            scheduler.shutdown(wait=False)


app = FastAPI(title="Post-Offer Engagement Platform", lifespan=lifespan)
register_exception_handlers(app)

API_V1_PREFIX = "/api/v1"
app.include_router(candidates.router, prefix=API_V1_PREFIX)
app.include_router(stages.router, prefix=API_V1_PREFIX)
app.include_router(interactions.router, prefix=API_V1_PREFIX)
app.include_router(recruiters.router, prefix=API_V1_PREFIX)
app.include_router(follow_up_actions.router, prefix=API_V1_PREFIX)
app.include_router(risk.router, prefix=API_V1_PREFIX)
app.include_router(ai.router, prefix=API_V1_PREFIX)
app.include_router(automation.router, prefix=API_V1_PREFIX)
app.include_router(analytics.router, prefix=API_V1_PREFIX)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
