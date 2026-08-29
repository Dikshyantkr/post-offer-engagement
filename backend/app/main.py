from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import models  # noqa: F401  (registers models on Base.metadata)
from app.db import Base, engine
from app.errors import register_exception_handlers
from app.routers import (
    ai,
    candidates,
    follow_up_actions,
    interactions,
    recruiters,
    risk,
    stages,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    Base.metadata.create_all(bind=engine)
    yield


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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
