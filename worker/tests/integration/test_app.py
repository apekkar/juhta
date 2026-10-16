"""The Worker's HTTP API end to end, with real git and a local bare repo as GitHub."""

import json
import subprocess
from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from worker import workspace
from worker.agent import AgentMessage, WorkerAgent, WorkerSettings
from worker.app import app
from worker.dependencies import get_worker
from worker.tests.fakes import ScriptedModel, agentcore_answering, github_mcp_answering

PR_URL = "https://github.com/octocat/juhta/pull/9"
TASK_7 = AgentMessage.model_validate({"from": "coordinator", "text": "implement #7"})
REVIEW_BRANCH = "worker/issue-7-1"


def summary_turn(changes: str) -> dict[str, object]:
    return {
        "tool": "WorkSummary",
        "input": {"changes": changes, "tests": "None needed.", "open_points": "None."},
    }


REVIEWED_PR = 47
ISSUE = {
    "title": "Add a greeting",
    "body": "Add hello.py.",
    "html_url": "https://github.com/octocat/juhta/issues/7",
}


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(  # noqa: S603 - test setup, fixed git commands
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A bare repo standing in for GitHub, with one commit on main."""
    seed, bare = tmp_path / "seed", tmp_path / "origin.git"
    seed.mkdir()
    git("init", "-q", "-b", "main", cwd=seed)
    (seed / "CLAUDE.md").write_text("# Rules\n")
    git("add", "-A", cwd=seed)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init", cwd=seed)
    git("clone", "-q", "--bare", str(seed), str(bare), cwd=tmp_path)
    return bare


@pytest.fixture(autouse=True)
def quick_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real checks need the real repo; here one passing command stands in for them."""
    monkeypatch.setattr(workspace, "SETUP", [])
    monkeypatch.setattr(workspace, "FORMATTERS", [])
    monkeypatch.setattr(workspace, "CHECKS", [["true"]])


@pytest.fixture(autouse=True)
def clear_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def boundaries() -> Iterator[tuple[MagicMock, MagicMock]]:
    github = github_mcp_answering(
        {"issue_read": {"issue": ISSUE}, "create_pull_request": {"url": PR_URL}}
    )
    agentcore = agentcore_answering({"reply": "Told the user."})
    with (
        patch("worker.agent.MCPClient", return_value=github),
        patch("worker.agent.boto3.client", return_value=agentcore),
    ):
        yield github, agentcore


def client_for(
    *, origin: Path, turns: list[dict[str, object] | str]
) -> tuple[TestClient, WorkerAgent]:
    settings = WorkerSettings(
        region="eu-west-1",
        github_token="github_pat_secret",
        github_repository="octocat/juhta",
        clone_url=str(origin),
        coordinator_runtime_arn="arn:coordinator",
    )
    worker = WorkerAgent(settings=settings, model=ScriptedModel(turns=turns))
    app.dependency_overrides[get_worker] = lambda: worker
    return TestClient(app), worker


def test_implements_the_issue_as_a_draft_pr_and_reports_to_coordinator(
    origin: Path, boundaries: tuple[MagicMock, MagicMock]
) -> None:
    github, agentcore = boundaries
    client, worker = client_for(
        origin=origin,
        turns=[
            {"tool": "read_file", "input": {"path": "CLAUDE.md"}},
            {"tool": "write_file", "input": {"path": "hello.py", "content": "print('hello')\n"}},
            "Done.",
            summary_turn("Added hello.py, which prints a greeting."),
        ],
    )

    response = client.post(
        "/invocations", json={"message": {"from": "coordinator", "text": "implement #7"}}
    )

    assert response.json() == {"reply": "Started on #7."}
    # TestClient runs the background task before returning: the whole issue has run.
    (branch,) = [
        line.split("refs/heads/")[1]
        for line in git("ls-remote", "--heads", str(origin), cwd=origin).splitlines()
        if "worker/issue-7-" in line
    ]
    assert git("show", f"{branch}:hello.py", cwd=origin) == "print('hello')\n"

    pr_call = github.call_tool_sync.call_args_list[-1].kwargs
    assert pr_call["name"] == "create_pull_request"
    assert pr_call["arguments"]["head"] == branch
    assert pr_call["arguments"]["draft"] is True
    body = pr_call["arguments"]["body"]
    assert body.startswith("Closes #7")
    assert "**What changed**\nAdded hello.py, which prints a greeting." in body
    assert "**Open points**\nNone." in body

    report = json.loads(agentcore.invoke_agent_runtime.call_args.kwargs["payload"])["message"]
    assert report["from"] == "worker"
    assert PR_URL in report["text"]
    assert "Added hello.py" in report["text"]
    assert not worker.busy


def test_reports_when_the_model_changed_nothing(
    origin: Path, boundaries: tuple[MagicMock, MagicMock]
) -> None:
    github, agentcore = boundaries
    client, _ = client_for(
        origin=origin, turns=["Nothing to change.", summary_turn("Nothing to change.")]
    )

    client.post("/invocations", json={"message": {"from": "coordinator", "text": "implement #7"}})

    assert [c.kwargs["name"] for c in github.call_tool_sync.call_args_list] == ["issue_read"]
    report = json.loads(agentcore.invoke_agent_runtime.call_args.kwargs["payload"])["message"]
    assert "changed nothing" in report["text"]


def test_ping_is_busy_while_an_issue_runs(origin: Path) -> None:
    client, worker = client_for(origin=origin, turns=[])
    assert client.get("/ping").json()["status"] == "Healthy"

    worker.accept(TASK_7)

    assert client.get("/ping").json()["status"] == "HealthyBusy"


def test_a_second_task_while_busy_is_refused(origin: Path) -> None:
    client, worker = client_for(origin=origin, turns=[])
    worker.accept(TASK_7)

    response = client.post(
        "/invocations", json={"message": {"from": "coordinator", "text": "implement #8"}}
    )

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()["detail"] == "Busy with #7"


@pytest.mark.parametrize(
    ("payload", "status"),
    [({"message": {"text": "implement #1"}}, 422), ({}, 400), ({"action": "deploy"}, 422)],
)
def test_malformed_invocations_are_rejected(
    origin: Path, payload: dict[str, object], status: int
) -> None:
    client, _ = client_for(origin=origin, turns=[])

    assert client.post("/invocations", json=payload).status_code == status


def test_ping_action_answers_without_work(origin: Path) -> None:
    client, worker = client_for(origin=origin, turns=[])

    assert client.post("/invocations", json={"action": "ping"}).json() == {"status": "ok"}
    assert not worker.busy


def test_reads_the_body_as_json_whatever_the_content_type(origin: Path) -> None:
    client, _ = client_for(origin=origin, turns=[])

    response = client.post(
        "/invocations",
        content=b'{"action": "ping"}',
        headers={"Content-Type": "application/octet-stream"},
    )

    assert response.json() == {"status": "ok"}


def push_worker_branch(origin: Path, *, tmp: Path) -> None:
    work = tmp / "earlier-run"
    git("clone", "-q", str(origin), str(work), cwd=tmp)
    git("checkout", "-q", "-b", REVIEW_BRANCH, cwd=work)
    (work / "hello.py").write_text("print('hello')\n")
    git("add", "-A", cwd=work)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "Implement #7", cwd=work)
    git("push", "-q", "origin", REVIEW_BRANCH, cwd=work)


def pull_request_read(*, head: str) -> object:
    answers = {
        "get": {"number": 47, "head": {"ref": head, "sha": "abc"}, "html_url": PR_URL},
        "get_reviews": [{"id": 1, "state": "CHANGES_REQUESTED", "body": "Greet the world."}],
        "get_review_comments": {
            "review_threads": [
                {
                    "id": "t1",
                    "is_resolved": False,
                    "is_outdated": False,
                    "comments": [{"path": "hello.py", "line": 1, "body": "Say 'hello, world'."}],
                }
            ],
            "totalCount": 1,
        },
    }
    return lambda arguments: answers[arguments["method"]]


def test_addresses_a_review_on_its_own_branch_and_reports(origin: Path, tmp_path: Path) -> None:
    push_worker_branch(origin, tmp=tmp_path)
    github = github_mcp_answering({"pull_request_read": pull_request_read(head=REVIEW_BRANCH)})
    agentcore = agentcore_answering({"reply": "Told the user."})
    client, worker = client_for(
        origin=origin,
        turns=[
            {
                "tool": "write_file",
                "input": {"path": "hello.py", "content": "print('hello, world')\n"},
            },
            "Done.",
            summary_turn("Greeting now says hello, world."),
        ],
    )
    with (
        patch("worker.agent.MCPClient", return_value=github),
        patch("worker.agent.boto3.client", return_value=agentcore),
    ):
        response = client.post(
            "/invocations",
            json={"message": {"from": "coordinator", "text": "address review on PR #47"}},
        )

    assert response.json() == {"reply": "Started on PR #47."}
    assert git("show", f"{REVIEW_BRANCH}:hello.py", cwd=origin) == "print('hello, world')\n"
    assert "Address review on PR #47" in git("log", "-1", "--format=%s", REVIEW_BRANCH, cwd=origin)
    task = worker.messages[0]["content"][0]["text"]
    assert "Greet the world." in task
    assert "hello.py:1: Say 'hello, world'." in task
    calls = [call.kwargs for call in github.call_tool_sync.call_args_list]
    assert {call["arguments"]["method"] for call in calls} == {
        "get",
        "get_reviews",
        "get_review_comments",
    }
    assert all(call["arguments"]["pullNumber"] == REVIEWED_PR for call in calls)
    report = json.loads(agentcore.invoke_agent_runtime.call_args.kwargs["payload"])["message"]
    assert "Addressed the review on PR #47" in report["text"]
    assert not worker.busy


def test_leaves_prs_that_arent_the_workers_alone(origin: Path) -> None:
    github = github_mcp_answering({"pull_request_read": pull_request_read(head="feature/mine")})
    agentcore = agentcore_answering({"reply": "Told the user."})
    client, _ = client_for(origin=origin, turns=[])
    with (
        patch("worker.agent.MCPClient", return_value=github),
        patch("worker.agent.boto3.client", return_value=agentcore),
    ):
        client.post(
            "/invocations",
            json={"message": {"from": "coordinator", "text": "address review on PR #47"}},
        )

    report = json.loads(agentcore.invoke_agent_runtime.call_args.kwargs["payload"])["message"]
    assert "isn't the Worker's" in report["text"]
