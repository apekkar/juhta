"""Health check: HealthyBusy keeps the AgentCore session alive while a task runs."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from ..agent import WorkerAgent
from ..dependencies import get_worker

router = APIRouter()


@router.get("/ping")
def ping(worker: Annotated[WorkerAgent, Depends(get_worker)]) -> dict[str, Any]:
    return {
        "status": "HealthyBusy" if worker.busy else "Healthy",
        "time_of_last_update": int(worker.status_changed_at),
    }
