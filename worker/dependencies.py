from fastapi import Request

from .agent import WorkerAgent


def get_worker(request: Request) -> WorkerAgent:
    worker: WorkerAgent = request.app.state.worker
    return worker
