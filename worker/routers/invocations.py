"""AgentCore Runtime's entrypoint: a Coordinator task message (run in the background) or ping.

The body is parsed as JSON regardless of Content-Type: InvokeAgentRuntime may not send one.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError

from ..agent import AgentMessage, TaskRejected, WorkerAgent
from ..dependencies import get_worker

router = APIRouter()


class Invocation(BaseModel):
    action: Literal["ping"] | None = None
    message: AgentMessage | None = None


@router.post("/invocations")
async def invoke(
    request: Request,
    background: BackgroundTasks,
    worker: Annotated[WorkerAgent, Depends(get_worker)],
) -> dict[str, str]:
    try:
        invocation = Invocation.model_validate_json(await request.body())
    except ValidationError as error:
        raise RequestValidationError(error.errors()) from error
    if invocation.action == "ping":
        return {"status": "ok"}
    if invocation.message is None:
        raise HTTPException(status_code=400, detail="Expected a message or {'action': 'ping'}")
    try:
        task = worker.accept(invocation.message)
    except TaskRejected as rejection:
        raise HTTPException(status_code=409, detail=str(rejection)) from rejection
    background.add_task(worker.run, task=task)
    return {"reply": f"Started on {task}."}
