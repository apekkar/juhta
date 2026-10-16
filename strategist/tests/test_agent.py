import json
from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from strands import Agent

from strategist.agent import AgentDesign, StrategistAgent

DESIGN = AgentDesign(
    name="InboxTriage",
    role="classifier",
    description="Sorts incoming email",
    system_prompt="You triage email.",
    capabilities=["classify", "route"],
    implementation_effort="low",
    plan_step="Outside the plan: personal email automation",
)
SECRET_ARN = "arn:aws:secretsmanager:eu-west-1:123:secret:juhta/strategist"
PLAN = "# Agent Pipeline Plan\n\nBuild the Worker agent next."


def design_event(
    *, request: str, design: AgentDesign, at: datetime | None = None
) -> dict[str, object]:
    return {
        "eventTimestamp": at or datetime(2026, 10, 1, tzinfo=UTC),
        "payload": [
            {"conversational": {"role": "USER", "content": {"text": request}}},
            {
                "conversational": {
                    "role": "ASSISTANT",
                    "content": {"text": design.model_dump_json()},
                }
            },
        ],
    }


@pytest.fixture
def memory_client() -> Iterator[MagicMock]:
    with patch("strategist.agent.MemoryClient") as client_cls:
        client_cls.return_value.list_events.return_value = []
        yield client_cls.return_value


@pytest.fixture
def mcp_client() -> Iterator[MagicMock]:
    with patch("strategist.agent.MCPClient") as client_cls:
        yield client_cls


@pytest.fixture
def github_tools() -> Iterator[MagicMock]:
    """Strands' direct tool caller (agent.tool), backed by the GitHub MCP server."""
    with patch.object(Agent, "tool", new_callable=PropertyMock) as tool:
        tool.return_value.get_file_contents.return_value = {
            "status": "success",
            "content": [{"text": "successfully downloaded text file"}, {"text": PLAN}],
        }
        yield tool.return_value


@pytest.fixture
def strategist(
    memory_client: MagicMock, mcp_client: MagicMock, github_tools: MagicMock
) -> StrategistAgent:
    return StrategistAgent(
        memory_id="mem-1",
        model_id="eu.anthropic.claude-sonnet-4-6",
        region="eu-west-1",
        github_repository="octocat/juhta",
        github_token="github_pat_test",
    )


def test_design_returns_structured_output(strategist: StrategistAgent) -> None:
    with patch.object(Agent, "__call__", return_value=MagicMock(structured_output=DESIGN)):
        assert strategist.design(workflow_description="Email triage", source="on-demand") == DESIGN


@pytest.mark.parametrize("source", ["daily", "on-demand"])
def test_design_is_stored_in_its_source_session(
    strategist: StrategistAgent, memory_client: MagicMock, source: str
) -> None:
    with patch.object(Agent, "__call__", return_value=MagicMock(structured_output=DESIGN)):
        strategist.design(workflow_description="Email triage", source=source)  # type: ignore[arg-type]

    memory_client.create_event.assert_called_once_with(
        memory_id="mem-1",
        actor_id="strategist",
        session_id=source,
        messages=[("Email triage", "USER"), (DESIGN.model_dump_json(), "ASSISTANT")],
    )


def test_design_sees_every_past_design_from_both_sessions(
    strategist: StrategistAgent, memory_client: MagicMock
) -> None:
    worker = DESIGN.model_copy(update={"name": "Worker", "plan_step": "Build order 3"})
    delegator = DESIGN.model_copy(update={"name": "Delegator", "plan_step": "Build order 7"})
    memory_client.list_events.side_effect = lambda **kwargs: {
        "daily": [design_event(request="daily", design=worker)],
        "on-demand": [design_event(request="manual", design=delegator)],
    }[kwargs["session_id"]]

    with patch.object(Agent, "__call__", return_value=MagicMock(structured_output=DESIGN)):
        strategist.design(workflow_description="Email triage", source="daily")

    prompt = str(strategist.system_prompt)
    assert "Worker [Build order 3]" in prompt
    assert "Delegator [Build order 7]" in prompt


def test_design_starts_from_an_empty_conversation(strategist: StrategistAgent) -> None:
    strategist.messages = [{"role": "user", "content": [{"text": "an earlier request"}]}]
    seen: list[object] = []

    def call(*args: object, **kwargs: object) -> MagicMock:
        seen.append(list(strategist.messages))
        return MagicMock(structured_output=DESIGN)

    with patch.object(Agent, "__call__", side_effect=call):
        strategist.design(workflow_description="Email triage", source="on-demand")

    assert seen == [[]]


def test_from_env_requires_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("AWS_REGION", "AGENT_SECRET_ARN", "AGENTCORE_MEMORY_ID"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(KeyError):
        StrategistAgent.from_env()


def test_reads_repository_through_read_only_github_mcp_server(
    strategist: StrategistAgent, mcp_client: MagicMock
) -> None:
    transport = mcp_client.call_args.args[0]
    with patch("strategist.agent.stdio_client") as stdio_client:
        transport()

    server = stdio_client.call_args.args[0]
    assert server.command == "github-mcp-server"
    assert "--read-only" in server.args
    assert "--toolsets=repos,issues" in server.args
    assert server.env == {"GITHUB_PERSONAL_ACCESS_TOKEN": "github_pat_test"}


def test_system_prompt_points_at_the_repository(strategist: StrategistAgent) -> None:
    assert "octocat/juhta" in str(strategist.system_prompt)


def test_from_env_reads_github_token_from_secrets_manager(
    monkeypatch: pytest.MonkeyPatch, memory_client: MagicMock, mcp_client: MagicMock
) -> None:
    for key, value in {
        "AGENTCORE_MEMORY_ID": "mem-1",
        "BEDROCK_MODEL_ID": "eu.anthropic.claude-sonnet-4-6",
        "AWS_REGION": "eu-west-1",
        "GITHUB_REPOSITORY": "octocat/juhta",
        "AGENT_SECRET_ARN": SECRET_ARN,
    }.items():
        monkeypatch.setenv(key, value)

    with patch("strategist.agent.boto3") as boto3:
        boto3.client.return_value.get_secret_value.return_value = {
            "SecretString": json.dumps({"github_pat": "github_pat_x"})
        }
        StrategistAgent.from_env()

    boto3.client.return_value.get_secret_value.assert_called_once_with(SecretId=SECRET_ARN)
    with patch("strategist.agent.stdio_client") as stdio_client:
        mcp_client.call_args.args[0]()
    assert stdio_client.call_args.args[0].env == {"GITHUB_PERSONAL_ACCESS_TOKEN": "github_pat_x"}


def test_design_reads_the_current_plan_into_the_system_prompt(
    strategist: StrategistAgent, github_tools: MagicMock
) -> None:
    with patch.object(Agent, "__call__", return_value=MagicMock(structured_output=DESIGN)):
        strategist.design(workflow_description="Email triage", source="on-demand")

    github_tools.get_file_contents.assert_called_once_with(
        owner="octocat",
        repo="juhta",
        path="docs/agent-pipeline-plan.md",
        record_direct_tool_call=False,
    )
    assert "Build the Worker agent next." in str(strategist.system_prompt)
    assert "You are the Strategist Agent" in str(strategist.system_prompt)


def test_read_plan_returns_the_plan_file(strategist: StrategistAgent) -> None:
    assert strategist.read_plan() == PLAN


def test_read_plan_fails_when_github_returns_an_error(
    strategist: StrategistAgent, github_tools: MagicMock
) -> None:
    github_tools.get_file_contents.return_value = {
        "status": "error",
        "content": [{"text": "failed to resolve git reference: 401 Bad credentials"}],
    }

    with pytest.raises(RuntimeError, match="401 Bad credentials"):
        strategist.read_plan()


def test_recent_designs_lists_both_sources_newest_first(
    strategist: StrategistAgent, memory_client: MagicMock
) -> None:
    worker = DESIGN.model_copy(update={"name": "Worker"})
    board = DESIGN.model_copy(update={"name": "Board"})
    memory_client.list_events.side_effect = lambda **kwargs: {
        "daily": [
            design_event(request="daily", design=worker, at=datetime(2026, 10, 6, tzinfo=UTC))
        ],
        "on-demand": [
            design_event(request="manual", design=board, at=datetime(2026, 10, 5, tzinfo=UTC))
        ],
    }[kwargs["session_id"]]

    records = strategist.recent_designs(limit=1)

    assert records == [
        {
            "source": "daily",
            "created_at": "2026-10-06T00:00:00+00:00",
            "request": "daily",
            "design": worker.model_dump(),
        }
    ]
