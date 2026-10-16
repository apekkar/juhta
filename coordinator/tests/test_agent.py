import io
import json
from collections.abc import Iterator
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from strands.hooks import BeforeToolCallEvent

from coordinator.agent import (
    SYSTEM_PROMPT,
    CoordinatorAgent,
    CoordinatorSettings,
    enforce_issue_policy,
    issue_write_violation,
    merge_blockers,
    run_daily,
)

AGENTCORE_MIN_SESSION_ID_LENGTH = 33
REPO = {"owner": "octocat", "repo": "juhta"}
SETTINGS = CoordinatorSettings(
    memory_id="mem-coordinator",
    model_id="eu.anthropic.claude-haiku-4-5-20251001-v1:0",
    region="eu-west-1",
    strategist_runtime_arn="arn:aws:bedrock-agentcore:eu-west-1:123:runtime/juhta_strategist-x",
    slack_bot_token="xoxb-test",
    slack_user_id="U123",
    github_token="github_pat_test",
    github_repository="octocat/juhta",
    worker_runtime_arn="arn:aws:bedrock-agentcore:eu-west-1:123:runtime/juhta_worker-x",
)


@pytest.fixture
def session_manager() -> Iterator[MagicMock]:
    with (
        patch("coordinator.agent.AgentCoreMemorySessionManager") as manager,
        patch("coordinator.agent.AgentCoreMemoryConfig") as config,
    ):
        manager.config = config
        yield manager


@pytest.fixture
def agentcore() -> Iterator[MagicMock]:
    with patch("coordinator.agent.boto3") as boto3:
        yield boto3.client.return_value


@pytest.fixture
def mcp_client() -> Iterator[MagicMock]:
    with patch("coordinator.agent.MCPClient") as client_cls:
        yield client_cls


@pytest.fixture
def coordinator(
    session_manager: MagicMock, agentcore: MagicMock, mcp_client: MagicMock
) -> CoordinatorAgent:
    return CoordinatorAgent(settings=SETTINGS, session_id="cli-test-session")


def strategist_answers(agentcore: MagicMock, answer: dict[str, object]) -> None:
    agentcore.invoke_agent_runtime.return_value = {
        "response": io.BytesIO(json.dumps(answer).encode())
    }


def test_remembers_the_conversation_per_session(
    session_manager: MagicMock, mcp_client: MagicMock
) -> None:
    with patch("coordinator.agent.boto3"):
        CoordinatorAgent(settings=SETTINGS, session_id="cli-test-session")

    session_manager.config.assert_called_once_with(
        memory_id="mem-coordinator", actor_id="user", session_id="cli-test-session"
    )


def test_has_routing_tools(coordinator: CoordinatorAgent) -> None:
    assert {"ask_strategist", "read_plan", "recent_designs", "send_dm", "queue_issue"} <= set(
        coordinator.tool_names
    )


def github_answers(mcp_client: MagicMock, answers: dict[str, object]) -> MagicMock:
    """GitHub MCP tools answer `answers[tool]` as JSON; for pull_request_read, a dict per
    `method`."""

    def answer(**call: Any) -> dict[str, object]:  # noqa: ANN401
        result = answers[call["name"]]
        if call["name"] == "pull_request_read":
            result = cast(dict[str, object], result)[call["arguments"]["method"]]
        return {"status": "success", "content": [{"text": json.dumps(result)}]}

    github: MagicMock = mcp_client.return_value
    github.__enter__.return_value = github
    github.call_tool_sync.side_effect = answer
    return github


def test_queue_issue_labels_it_and_hands_it_to_the_worker(
    coordinator: CoordinatorAgent, mcp_client: MagicMock, agentcore: MagicMock
) -> None:
    github = github_answers(
        mcp_client, {"issue_read": {"issue": {"labels": ["bug"]}}, "issue_write": {}}
    )
    strategist_answers(agentcore, {"reply": "Started on #41."})

    result = coordinator.tool.queue_issue(issue_number=41, record_direct_tool_call=False)

    calls = [call.kwargs for call in github.call_tool_sync.call_args_list]
    assert [call["name"] for call in calls] == ["issue_read", "issue_write"]
    assert calls[1]["arguments"] == {
        **REPO,
        "method": "update",
        "issue_number": 41,
        "labels": ["bug", "queued"],
    }
    request = agentcore.invoke_agent_runtime.call_args.kwargs
    assert request["agentRuntimeArn"] == SETTINGS.worker_runtime_arn
    assert json.loads(request["payload"]) == {
        "message": {"from": "coordinator", "text": "implement #41"}
    }
    assert "Started on #41." in str(result["content"])


def test_queue_issue_fails_when_the_worker_refuses(
    coordinator: CoordinatorAgent, mcp_client: MagicMock, agentcore: MagicMock
) -> None:
    github_answers(mcp_client, {"issue_read": {"issue": {}}, "issue_write": {}})
    agentcore.invoke_agent_runtime.side_effect = RuntimeError("409 Busy with #40")

    result = coordinator.tool.queue_issue(issue_number=41, record_direct_tool_call=False)

    assert result["status"] == "error"
    assert "Busy with #40" in str(result["content"])


def test_address_review_sends_the_worker_back_to_its_pr(
    coordinator: CoordinatorAgent, agentcore: MagicMock
) -> None:
    strategist_answers(agentcore, {"reply": "Started on PR #47."})

    result = coordinator.tool.address_review(pr_number=47, record_direct_tool_call=False)

    request = agentcore.invoke_agent_runtime.call_args.kwargs
    assert request["agentRuntimeArn"] == SETTINGS.worker_runtime_arn
    assert json.loads(request["payload"]) == {
        "message": {"from": "coordinator", "text": "address review on PR #47"}
    }
    assert "Started on PR #47." in str(result["content"])


def test_read_only_coordinator_may_send_the_worker_to_a_review(
    session_manager: MagicMock, agentcore: MagicMock, mcp_client: MagicMock
) -> None:
    coordinator = CoordinatorAgent(settings=SETTINGS, session_id="slack-U123", read_only=True)
    event = BeforeToolCallEvent(
        agent=coordinator,
        selected_tool=None,
        tool_use={"toolUseId": "t1", "name": "address_review", "input": {"pr_number": 47}},
        invocation_state={},
    )

    coordinator.hooks.invoke_callbacks(event)

    assert not event.cancel_tool


def test_prompt_sends_the_worker_to_requested_changes() -> None:
    assert "address_review" in SYSTEM_PROMPT


def test_read_only_coordinator_cannot_queue(
    session_manager: MagicMock, agentcore: MagicMock, mcp_client: MagicMock
) -> None:
    coordinator = CoordinatorAgent(settings=SETTINGS, session_id="slack-U123", read_only=True)
    event = BeforeToolCallEvent(
        agent=coordinator,
        selected_tool=None,
        tool_use={"toolUseId": "t1", "name": "queue_issue", "input": {"issue_number": 41}},
        invocation_state={},
    )

    coordinator.hooks.invoke_callbacks(event)

    assert event.cancel_tool


def test_recent_designs_asks_strategist_for_its_designs(
    coordinator: CoordinatorAgent, agentcore: MagicMock
) -> None:
    strategist_answers(agentcore, {"designs": [{"source": "daily", "design": {"name": "Worker"}}]})

    result = coordinator.tool.recent_designs(limit=3, record_direct_tool_call=False)

    assert json.loads(agentcore.invoke_agent_runtime.call_args.kwargs["payload"]) == {
        "action": "designs",
        "limit": 3,
    }
    assert "Worker" in str(result["content"])


def test_blocks_closing_an_issue_through_its_hooks(coordinator: CoordinatorAgent) -> None:
    event = BeforeToolCallEvent(
        agent=coordinator,
        selected_tool=None,
        tool_use={
            "toolUseId": "t1",
            "name": "issue_write",
            "input": {
                "owner": "octocat",
                "repo": "juhta",
                "method": "update",
                "issue_number": 23,
                "state": "closed",
            },
        },
        invocation_state={},
    )

    coordinator.hooks.invoke_callbacks(event)

    assert event.cancel_tool


def test_read_only_coordinator_blocks_even_allowed_issue_writes(
    session_manager: MagicMock, agentcore: MagicMock, mcp_client: MagicMock
) -> None:
    coordinator = CoordinatorAgent(settings=SETTINGS, session_id="slack-U123", read_only=True)
    event = BeforeToolCallEvent(
        agent=coordinator,
        selected_tool=None,
        tool_use={
            "toolUseId": "t1",
            "name": "issue_write",
            "input": {"owner": "octocat", "repo": "juhta", "method": "create", "title": "x"},
        },
        invocation_state={},
    )

    coordinator.hooks.invoke_callbacks(event)

    assert event.cancel_tool


def test_ask_strategist_invokes_the_strategist_runtime(
    coordinator: CoordinatorAgent, agentcore: MagicMock
) -> None:
    strategist_answers(agentcore, {"design": {"name": "Worker"}})

    result = coordinator.tool.ask_strategist(
        request="Design a worker", record_direct_tool_call=False
    )

    call = agentcore.invoke_agent_runtime.call_args.kwargs
    assert call["agentRuntimeArn"] == SETTINGS.strategist_runtime_arn
    assert json.loads(call["payload"]) == {"prompt": "Design a worker"}
    assert len(call["runtimeSessionId"]) >= AGENTCORE_MIN_SESSION_ID_LENGTH
    assert "Worker" in str(result["content"])


def test_read_plan_asks_strategist_for_the_plan(
    coordinator: CoordinatorAgent, agentcore: MagicMock
) -> None:
    strategist_answers(agentcore, {"plan": "# Plan"})

    result = coordinator.tool.read_plan(record_direct_tool_call=False)

    assert json.loads(agentcore.invoke_agent_runtime.call_args.kwargs["payload"]) == {
        "action": "plan"
    }
    assert "# Plan" in str(result["content"])


def slack_answers(body: bytes) -> MagicMock:
    response = MagicMock()
    response.__enter__.return_value.read.return_value = body
    return response


def test_send_dm_posts_to_the_user_with_the_bot_token(coordinator: CoordinatorAgent) -> None:
    with patch(
        "coordinator.agent.urllib.request.urlopen", return_value=slack_answers(b'{"ok": true}')
    ) as urlopen:
        result = coordinator.tool.send_dm(text="Hello", record_direct_tool_call=False)

    request = urlopen.call_args.args[0]
    assert request.full_url == "https://slack.com/api/chat.postMessage"
    assert request.headers["Authorization"] == "Bearer xoxb-test"
    assert json.loads(request.data) == {"channel": "U123", "text": "Hello"}
    assert result["status"] == "success"


def test_send_dm_fails_when_slack_rejects_the_message(coordinator: CoordinatorAgent) -> None:
    with patch(
        "coordinator.agent.urllib.request.urlopen",
        return_value=slack_answers(b'{"ok": false, "error": "invalid_auth"}'),
    ):
        result = coordinator.tool.send_dm(text="Hello", record_direct_tool_call=False)

    assert result["status"] == "error"
    assert "invalid_auth" in str(result["content"])


def test_settings_read_the_bot_token_from_the_coordinator_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in {
        "AGENTCORE_MEMORY_ID": "mem-coordinator",
        "COORDINATOR_MODEL_ID": "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
        "AWS_REGION": "eu-west-1",
        "STRATEGIST_RUNTIME_ARN": SETTINGS.strategist_runtime_arn,
        "AGENT_SECRET_ARN": "arn:aws:secretsmanager:eu-west-1:123:secret:juhta/coordinator",
        "SLACK_USER_ID": "U123",
        "GITHUB_REPOSITORY": "octocat/juhta",
        "WORKER_RUNTIME_ARN": SETTINGS.worker_runtime_arn,
    }.items():
        monkeypatch.setenv(key, value)

    with patch("coordinator.agent.boto3") as boto3:
        boto3.client.return_value.get_secret_value.return_value = {
            "SecretString": json.dumps(
                {
                    "slack_bot_token": "xoxb-test",
                    "slack_signing_secret": "unused-here",
                    "github_pat": "github_pat_test",
                }
            )
        }
        assert CoordinatorSettings.from_env() == SETTINGS


def test_prompt_asks_for_slack_formatting_and_only_tool_backed_offers() -> None:
    assert "mrkdwn" in SYSTEM_PROMPT
    assert "Only offer actions your tools can do" in SYSTEM_PROMPT


def test_run_daily_offers_the_design_in_the_users_conversation(agentcore: MagicMock) -> None:
    design = {
        "name": "Worker",
        "description": "Implements queued issues and opens PRs.",
        "plan_step": "Build order 6: Worker agent",
        "implementation_effort": "high",
        "capabilities": ["implement", "open PRs"],
    }
    strategist_answers(agentcore, {"design": design})

    with (
        patch("coordinator.agent.CoordinatorAgent") as agent_cls,
        patch("coordinator.agent.post_slack_message") as post,
    ):
        agent_cls.return_value.return_value = "Today's idea: Worker… File it and start? (go)"
        assert run_daily(settings=SETTINGS) == design

    assert json.loads(agentcore.invoke_agent_runtime.call_args.kwargs["payload"]) == {
        "action": "daily"
    }
    agent_cls.assert_called_once_with(settings=SETTINGS, session_id="slack-U123", read_only=True)
    prompt = agent_cls.return_value.call_args.args[0]
    assert "Build order 6: Worker agent" in prompt
    assert "go" in prompt
    post.assert_called_once_with(
        token="xoxb-test", channel="U123", text="Today's idea: Worker… File it and start? (go)"
    )


def test_prompt_lets_a_go_on_a_proposal_file_and_queue_in_one_turn() -> None:
    assert "file it and start" in SYSTEM_PROMPT


def mcp_servers(mcp_client: MagicMock) -> dict[str, tuple[Any, dict[str, object]]]:
    """Each GitHub MCP client Coordinator builds: toolsets → (server, tool filters)."""
    servers = {}
    for call in mcp_client.call_args_list:
        with patch("coordinator.agent.stdio_client") as stdio_client:
            call.args[0]()
        server = stdio_client.call_args.args[0]
        servers[server.args[1]] = (server, call.kwargs["tool_filters"])
    return servers


def test_reads_and_writes_issues_through_the_github_mcp_server(
    coordinator: CoordinatorAgent, mcp_client: MagicMock
) -> None:
    server, filters = mcp_servers(mcp_client)["--toolsets=issues"]

    assert server.command == "github-mcp-server"
    assert server.env == {"GITHUB_PERSONAL_ACCESS_TOKEN": "github_pat_test"}
    assert filters == {"allowed": ["issue_read", "list_issues", "search_issues", "issue_write"]}


def test_merges_through_a_separate_pull_requests_client(
    coordinator: CoordinatorAgent, mcp_client: MagicMock
) -> None:
    _, filters = mcp_servers(mcp_client)["--toolsets=pull_requests"]

    assert filters == {"allowed": ["pull_request_read", "merge_pull_request"]}
    assert "merge_pull_request" not in coordinator.tool_names


def test_issue_policy_allows_creating_issues() -> None:
    tool_input = {**REPO, "method": "create", "title": "Add Worker", "body": "## Goal"}

    assert issue_write_violation(tool_input=tool_input, repository="octocat/juhta") is None


@pytest.mark.parametrize(
    "tool_input",
    [
        {**REPO, "method": "create", "title": "x", "body": "y", "labels": ["queued"]},
        {**REPO, "method": "update", "issue_number": 23, "state": "closed"},
        {**REPO, "method": "update", "issue_number": 23, "body": "rewritten"},
        {**REPO, "method": "update", "issue_number": 23, "labels": ["bug"]},
        {**REPO, "method": "update", "issue_number": 23, "labels": ["queued"]},
        {"owner": "someone", "repo": "else", "method": "create", "title": "x", "body": "y"},
    ],
)
def test_issue_policy_blocks_everything_else(tool_input: dict[str, object]) -> None:
    assert issue_write_violation(tool_input=tool_input, repository="octocat/juhta")


def test_hook_cancels_a_blocked_issue_write() -> None:
    event = MagicMock(
        tool_use={
            "name": "issue_write",
            "input": {**REPO, "method": "update", "issue_number": 23, "state": "closed"},
        },
        cancel_tool=False,
    )

    enforce_issue_policy(event, repository="octocat/juhta")

    assert event.cancel_tool


def test_prompt_requires_explicit_approval_before_queueing() -> None:
    assert "explicit" in SYSTEM_PROMPT
    assert "queue_issue" in SYSTEM_PROMPT


def test_prompt_never_files_done_or_duplicate_work() -> None:
    assert "Done" in SYSTEM_PROMPT
    assert "search_issues" in SYSTEM_PROMPT
    assert "ask_strategist" in SYSTEM_PROMPT


def test_prompt_builds_issues_from_strategist_designs() -> None:
    assert "recent_designs" in SYSTEM_PROMPT
    assert "plan_step" in SYSTEM_PROMPT


# Shapes recorded from GitHub's MCP server.
def pr(**overrides: object) -> dict[str, object]:
    return {"number": 47, "state": "open", "draft": False, "html_url": "https://x/47", **overrides}


def approval(state: str = "APPROVED", login: str = "octocat") -> dict[str, object]:
    return {"id": 1, "state": state, "user": {"login": login}, "html_url": ""}


def check(
    name: str, *, status: str = "completed", conclusion: str | None = "success"
) -> dict[str, object]:
    return {"name": name, "status": status, "conclusion": conclusion, "html_url": ""}


GREEN = {"check_runs": [check("python-unit-tests"), check("security-checks")]}


def test_nothing_blocks_merging_an_approved_green_pr() -> None:
    assert merge_blockers(pr=pr(), reviews=[approval()], check_runs=GREEN, owner="octocat") == []


@pytest.mark.parametrize(
    ("kwargs", "blocker"),
    [
        ({"pr": pr(draft=True)}, "still a draft"),
        ({"pr": pr(state="closed")}, "it's closed"),
        ({"reviews": []}, "you haven't approved it"),
        ({"reviews": [approval(login="someone")]}, "you haven't approved it"),
        # The latest review counts: approving, then requesting changes, isn't an approval.
        ({"reviews": [approval(), approval(state="CHANGES_REQUESTED")]}, "you haven't approved"),
        (
            {"check_runs": {"check_runs": [check("tests", status="in_progress", conclusion=None)]}},
            "checks still running: tests",
        ),
        (
            {"check_runs": {"check_runs": [check("tests", conclusion="failure")]}},
            "checks failed: tests",
        ),
        ({"check_runs": {"check_runs": []}}, "no checks ran"),
    ],
)
def test_merge_is_blocked(kwargs: dict[str, Any], blocker: str) -> None:
    args = {"pr": pr(), "reviews": [approval()], "check_runs": GREEN, "owner": "octocat", **kwargs}

    assert any(blocker in reason for reason in merge_blockers(**args))


def test_merge_pr_merges_an_approved_green_pr(
    coordinator: CoordinatorAgent, mcp_client: MagicMock
) -> None:
    github = github_answers(
        mcp_client,
        {
            "pull_request_read": {
                "get": pr(),
                "get_reviews": [approval()],
                "get_check_runs": GREEN,
            },
            "merge_pull_request": {"sha": "abc", "merged": True, "message": "merged"},
        },
    )

    result = coordinator.tool.merge_pr(pr_number=47, record_direct_tool_call=False)

    merge = github.call_tool_sync.call_args_list[-1].kwargs
    assert merge["name"] == "merge_pull_request"
    assert merge["arguments"] == {**REPO, "pullNumber": 47, "merge_method": "rebase"}
    assert "Merged PR #47" in str(result["content"])


def test_merge_pr_refuses_without_the_users_approval(
    coordinator: CoordinatorAgent, mcp_client: MagicMock
) -> None:
    github = github_answers(
        mcp_client,
        {"pull_request_read": {"get": pr(), "get_reviews": [], "get_check_runs": GREEN}},
    )

    result = coordinator.tool.merge_pr(pr_number=47, record_direct_tool_call=False)

    assert "Not merging PR #47" in str(result["content"])
    names = [call.kwargs["name"] for call in github.call_tool_sync.call_args_list]
    assert "merge_pull_request" not in names


def test_read_only_coordinator_cannot_merge(
    session_manager: MagicMock, agentcore: MagicMock, mcp_client: MagicMock
) -> None:
    coordinator = CoordinatorAgent(settings=SETTINGS, session_id="slack-U123", read_only=True)
    event = BeforeToolCallEvent(
        agent=coordinator,
        selected_tool=None,
        tool_use={"toolUseId": "t1", "name": "merge_pr", "input": {"pr_number": 47}},
        invocation_state={},
    )

    coordinator.hooks.invoke_callbacks(event)

    assert event.cancel_tool


def test_prompt_asks_before_merging() -> None:
    assert "merge #N?" in SYSTEM_PROMPT
