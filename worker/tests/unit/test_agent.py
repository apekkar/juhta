import json
from unittest.mock import MagicMock, patch

import pytest

from worker.agent import (
    SYSTEM_PROMPT,
    AgentMessage,
    Task,
    TaskRejected,
    WorkerAgent,
    WorkerSettings,
    WorkSummary,
    call_github,
    github_mcp,
    review_feedback,
)
from worker.tests.fakes import ScriptedModel, agentcore_answering

AGENTCORE_MIN_SESSION_ID_LENGTH = 33
SETTINGS = WorkerSettings(
    region="eu-west-1",
    github_token="github_pat_secret",
    github_repository="octocat/juhta",
    clone_url="https://x-access-token:github_pat_secret@github.com/octocat/juhta.git",
    coordinator_runtime_arn="arn:aws:bedrock-agentcore:eu-west-1:123:runtime/juhta_coordinator-x",
)


@pytest.fixture
def worker() -> WorkerAgent:
    return WorkerAgent(settings=SETTINGS, model=ScriptedModel(turns=[]))


def task(text: str, *, sender: str = "coordinator") -> AgentMessage:
    return AgentMessage.model_validate({"from": sender, "text": text})


@pytest.mark.parametrize(("text", "number"), [("implement #28", 28), ("Implement issue #7", 7)])
def test_accepts_coordinators_task_and_becomes_busy(
    worker: WorkerAgent, text: str, number: int
) -> None:
    assert worker.accept(task(text)) == Task(kind="implement", number=number)
    assert worker.busy


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        (task("implement #1", sender="worker"), "from Coordinator only"),
        (task("look at #3"), "Expected 'implement #<issue>' or 'address review on PR #<n>'"),
    ],
)
def test_rejects_other_messages(worker: WorkerAgent, message: AgentMessage, reason: str) -> None:
    with pytest.raises(TaskRejected, match=reason):
        worker.accept(message)
    assert not worker.busy


def test_takes_one_issue_at_a_time(worker: WorkerAgent) -> None:
    worker.accept(task("implement #1"))

    with pytest.raises(TaskRejected, match="Busy with #1"):
        worker.accept(task("implement #2"))


def test_file_tools_need_a_checked_out_issue(worker: WorkerAgent) -> None:
    result = worker.tool.read_file(path="README.md", record_direct_tool_call=False)

    assert result["status"] == "error"
    assert "No issue is checked out" in str(result["content"])


def test_reports_to_coordinator_with_the_message_envelope(worker: WorkerAgent) -> None:
    agentcore = agentcore_answering({"reply": "Told the user."})
    with patch("worker.agent.boto3.client", return_value=agentcore):
        worker.report(text="Draft PR: https://x")

    request = agentcore.invoke_agent_runtime.call_args.kwargs
    assert request["agentRuntimeArn"] == SETTINGS.coordinator_runtime_arn
    assert len(request["runtimeSessionId"]) >= AGENTCORE_MIN_SESSION_ID_LENGTH
    assert json.loads(request["payload"]) == {
        "message": {"from": "worker", "text": "Draft PR: https://x"}
    }


def test_report_fails_when_coordinator_rejects_it(worker: WorkerAgent) -> None:
    agentcore = agentcore_answering({"error": "message needs 'from' and 'text' strings"})
    with (
        patch("worker.agent.boto3.client", return_value=agentcore),
        pytest.raises(RuntimeError, match="message needs"),
    ):
        worker.report(text="x")


def test_a_failed_run_is_reported_without_the_token_and_frees_the_worker(
    worker: WorkerAgent,
) -> None:
    agentcore = agentcore_answering({"reply": "ok"})
    github = MagicMock()
    github.__enter__.return_value = github
    github.call_tool_sync.side_effect = RuntimeError(
        "https://x-access-token:github_pat_secret@github.com failed"
    )
    worker.accept(task("implement #28"))
    with (
        patch("worker.agent.MCPClient", return_value=github),
        patch("worker.agent.boto3.client", return_value=agentcore),
    ):
        worker.run(task=Task(kind="implement", number=28))

    payload = json.loads(agentcore.invoke_agent_runtime.call_args.kwargs["payload"])
    assert "Worker failed on #28" in payload["message"]["text"]
    assert "github_pat_secret" not in payload["message"]["text"]
    assert not worker.busy


def test_github_goes_through_the_mcp_server_limited_to_issues_and_prs_it_needs() -> None:
    with patch("worker.agent.MCPClient") as client_cls:
        github_mcp(settings=SETTINGS)

    assert client_cls.call_args.kwargs["tool_filters"] == {
        "allowed": ["issue_read", "create_pull_request", "pull_request_read"]
    }


def test_call_github_returns_the_tools_json() -> None:
    github = MagicMock()
    github.call_tool_sync.return_value = {
        "status": "success",
        "content": [{"text": '{"url": "https://github.com/octocat/juhta/pull/9"}'}],
    }

    result = call_github(github=github, tool="create_pull_request", arguments={"title": "x"})

    assert result == {"url": "https://github.com/octocat/juhta/pull/9"}
    assert github.call_tool_sync.call_args.kwargs["name"] == "create_pull_request"


def test_call_github_fails_on_a_tool_error() -> None:
    github = MagicMock()
    github.call_tool_sync.return_value = {"status": "error", "content": [{"text": "Not Found"}]}

    with pytest.raises(RuntimeError, match="issue_read failed: Not Found"):
        call_github(github=github, tool="issue_read", arguments={})


def test_settings_find_coordinator_through_its_ssm_parameter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in {
        "AWS_REGION": "eu-west-1",
        "AGENT_SECRET_ARN": "juhta/worker",
        "GITHUB_REPOSITORY": "octocat/juhta",
        "COORDINATOR_RUNTIME_PARAMETER": "/juhta/runtimes/coordinator",
    }.items():
        monkeypatch.setenv(key, value)
    aws = MagicMock()
    aws.get_secret_value.return_value = {"SecretString": '{"github_pat": "github_pat_secret"}'}
    aws.get_parameter.return_value = {"Parameter": {"Value": SETTINGS.coordinator_runtime_arn}}

    with patch("worker.agent.boto3.client", return_value=aws):
        assert WorkerSettings.from_env() == SETTINGS

    aws.get_parameter.assert_called_once_with(Name="/juhta/runtimes/coordinator")


@pytest.mark.parametrize("text", ["address review on PR #47", "Address review on #47"])
def test_accepts_coordinators_review_task(worker: WorkerAgent, text: str) -> None:
    accepted = worker.accept(task(text))

    assert accepted == Task(kind="review", number=47)
    assert str(accepted) == "PR #47"
    assert worker.busy


# Shapes recorded from the real GitHub MCP server.
CHANGES_REQUESTED = {
    "id": 1,
    "state": "CHANGES_REQUESTED",
    "body": "Use a constant.",
    "html_url": "",
}
APPROVED = {"id": 2, "state": "APPROVED", "body": "Looks good otherwise.", "html_url": ""}


def thread(*, path: str, line: int | None, body: str, **flags: bool) -> dict[str, object]:
    comment = {"path": path, "line": line, "body": body, "author": "octocat", "html_url": ""}
    return {
        "id": "t",
        "is_resolved": flags.get("resolved", False),
        "is_outdated": flags.get("outdated", False),
        "is_collapsed": False,
        "comments": [comment],
        "total_count": 1,
    }


def test_review_feedback_lists_change_requests_and_open_inline_comments() -> None:
    feedback = review_feedback(
        reviews=[CHANGES_REQUESTED, APPROVED],
        threads=[
            thread(path="worker/agent.py", line=12, body="Rename this."),
            thread(path="README.md", line=None, body="Mention it here."),
            thread(path="old.py", line=3, body="Done already.", resolved=True),
            thread(path="gone.py", line=3, body="Code moved.", outdated=True),
        ],
    )

    assert feedback == (
        "- Use a constant.\n- worker/agent.py:12: Rename this.\n- README.md: Mention it here."
    )


def test_review_feedback_is_empty_without_open_requests() -> None:
    assert review_feedback(reviews=[APPROVED], threads=[]) == ""


def test_work_summary_reads_as_sections() -> None:
    summary = WorkSummary(changes="Added X.", tests="test_x covers it.", open_points="None.")

    assert summary.as_markdown() == (
        "**What changed**\nAdded X.\n\n**Tests**\ntest_x covers it.\n\n**Open points**\nNone."
    )


def test_prompt_forbids_inventing_external_api_shapes() -> None:
    assert "never invent" in SYSTEM_PROMPT
