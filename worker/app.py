"""The Worker's AgentCore Runtime app (POST /invocations, GET /ping)."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from strands.models import BedrockModel

from .agent import WorkerAgent, WorkerSettings
from .routers import invocations, ping


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = WorkerSettings.from_env()
    app.state.worker = WorkerAgent(
        settings=settings,
        model=BedrockModel(model_id=os.environ["WORKER_MODEL_ID"], region_name=settings.region),
    )
    yield


app = FastAPI(lifespan=_lifespan)
app.include_router(ping.router)
app.include_router(invocations.router)
