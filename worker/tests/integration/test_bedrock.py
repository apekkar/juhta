"""The real Bedrock model, git and checks implement a small issue in a bare clone of this repo.

Only GitHub's MCP server and Coordinator are faked. Run: `uv run pytest -m bedrock`.
"""

import json
import os
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from strands.models import BedrockModel

from worker.agent import WorkerAgent, WorkerSettings
from worker.dependencies import get_worker
from worker.routers import invocations, ping
from worker.tests.fakes import agentcore_answering, github_mcp_answering

pytestmark = pytest.mark.bedrock

REPO_ROOT = Path(__file__).resolve().parents[3]
RUN_TIMEOUT_SECONDS = 25 * 60
ISSUE = {
    "title": "Add a slugify helper",
    "html_url": "https://github.com/octocat/juhta/issues/999",
    "body": (
        "Add a module `worker/slug.py` with a function `slugify(*, text: str) -> str` that "
        "lowercases the text, replaces every run of characters other than a-z and 0-9 with a "
        "single hyphen, and strips hyphens from both ends. Example: "
        '`slugify(text="  Hello, World!  ")` returns `"hello-world"`. '
        "Add unit tests in `worker/tests/unit/test_slug.py`."
    ),
}


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(  # noqa: S603 - fixed git commands
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    git("clone", "-q", "--bare", "--no-local", str(REPO_ROOT), str(bare), cwd=tmp_path)
    return bare


@pytest.fixture
def boundaries() -> Iterator[tuple[MagicMock, MagicMock]]:
    # Patching boto3.client leaves Bedrock real: BedrockModel uses its own boto3 session.
    github = github_mcp_answering(
        {
            "issue_read": {"issue": ISSUE},
            "create_pull_request": {"url": "https://example.test/pull/1"},
        }
    )
    coordinator = agentcore_answering({"reply": "Told the user."})
    with (
        patch("worker.agent.MCPClient", return_value=github),
        patch("worker.agent.boto3.client", return_value=coordinator),
    ):
        yield github, coordinator


@pytest.fixture
def served(origin: Path, github_token: str) -> Iterator[tuple[str, WorkerAgent]]:
    region = os.environ["AWS_REGION"]
    worker = WorkerAgent(
        settings=WorkerSettings(
            region=region,
            github_token=github_token,
            github_repository=os.environ["GITHUB_REPOSITORY"],
            clone_url=str(origin),
            coordinator_runtime_arn=os.environ["COORDINATOR_RUNTIME_ARN"],
        ),
        model=BedrockModel(model_id=os.environ["BEDROCK_MODEL_ID"], region_name=region),
    )
    app = FastAPI()
    app.include_router(ping.router)
    app.include_router(invocations.router)
    app.dependency_overrides[get_worker] = lambda: worker
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", worker
    server.should_exit = True
    thread.join(timeout=10)


def test_worker_implements_an_issue_with_passing_checks(
    served: tuple[str, WorkerAgent], origin: Path, boundaries: tuple[MagicMock, MagicMock]
) -> None:
    url, _ = served
    github, coordinator = boundaries

    started = httpx.post(
        f"{url}/invocations",
        json={"message": {"from": "coordinator", "text": "implement #999"}},
        timeout=30,
    )
    assert started.json() == {"reply": "Started on #999."}
    assert httpx.get(f"{url}/ping").json()["status"] == "HealthyBusy"

    deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
    while httpx.get(f"{url}/ping").json()["status"] == "HealthyBusy":
        assert time.monotonic() < deadline, "Worker didn't finish in time"
        time.sleep(5)

    report = json.loads(coordinator.invoke_agent_runtime.call_args.kwargs["payload"])["message"]
    assert "All checks pass." in report["text"], report["text"]

    pr = github.call_tool_sync.call_args_list[-1].kwargs
    assert pr["name"] == "create_pull_request"
    assert pr["arguments"]["draft"] is True
    assert pr["arguments"]["body"].startswith("Closes #999")

    branch = pr["arguments"]["head"]
    namespace: dict[str, Any] = {}
    exec(git("show", f"{branch}:worker/slug.py", cwd=origin), namespace)  # noqa: S102 - the Worker's own output
    assert namespace["slugify"](text="  Hello, World!  ") == "hello-world"
    assert "def test_" in git("show", f"{branch}:worker/tests/unit/test_slug.py", cwd=origin)
