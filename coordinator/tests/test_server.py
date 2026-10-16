import importlib
import sys
from collections.abc import Iterator
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def agent_cls() -> Iterator[MagicMock]:
    sys.modules.pop("coordinator.server", None)
    with (
        patch("coordinator.agent.CoordinatorSettings.from_env") as settings,
        patch("coordinator.agent.CoordinatorAgent") as agent_cls,
    ):
        settings.return_value = MagicMock(slack_bot_token="xoxb-test", slack_user_id="U123")
        yield agent_cls
    sys.modules.pop("coordinator.server", None)


@pytest.fixture
def server(agent_cls: MagicMock) -> ModuleType:
    return importlib.import_module("coordinator.server")


def test_prompt_is_answered_in_its_session(server: ModuleType, agent_cls: MagicMock) -> None:
    agent_cls.return_value.return_value = "Here is the design."

    reply = server.invoke({"prompt": "Design a worker", "session_id": "cli-test-session"})

    assert reply == {"reply": "Here is the design."}
    agent_cls.assert_called_once_with(settings=server.settings, session_id="cli-test-session")
    agent_cls.return_value.assert_called_once_with("Design a worker")


def test_slack_dm_is_answered_in_the_dm(server: ModuleType, agent_cls: MagicMock) -> None:
    agent_cls.return_value.return_value = "Strategist suggests a Worker agent."
    with patch("coordinator.agent.post_slack_message") as post:
        server.invoke({"slack_event": {"channel": "D999", "text": "What next?"}})

    agent_cls.assert_called_once_with(settings=server.settings, session_id="slack-U123")
    agent_cls.return_value.assert_called_once_with("What next?")
    post.assert_called_once_with(
        token="xoxb-test", channel="D999", text="Strategist suggests a Worker agent."
    )


def test_daily_action_runs_the_daily_summary(server: ModuleType) -> None:
    with patch("coordinator.agent.run_daily", return_value={"name": "Worker"}) as run_daily:
        assert server.invoke({"action": "daily"}) == {"design": {"name": "Worker"}}

    run_daily.assert_called_once_with(settings=server.settings)


def test_ping_action_answers_without_running_the_agent(
    server: ModuleType, agent_cls: MagicMock
) -> None:
    assert server.invoke({"action": "ping"}) == {"status": "ok"}
    agent_cls.assert_not_called()


def test_agent_message_is_read_in_the_users_conversation_read_only_and_dmd(
    server: ModuleType, agent_cls: MagicMock
) -> None:
    agent_cls.return_value.return_value = "Worker finished #28: PR ready."
    with patch("coordinator.agent.post_slack_message") as post:
        reply = server.invoke({"message": {"from": "worker", "text": "Done with #28."}})

    assert reply == {"reply": "Worker finished #28: PR ready."}
    agent_cls.assert_called_once_with(
        settings=server.settings, session_id="slack-U123", read_only=True
    )
    prompt = agent_cls.return_value.call_args.args[0]
    assert "Message from worker" in prompt
    assert "Done with #28." in prompt
    post.assert_called_once_with(
        token="xoxb-test", channel="U123", text="Worker finished #28: PR ready."
    )


@pytest.mark.parametrize(
    "message", [None, "hi", {"text": "no sender"}, {"from": "worker"}, {"from": 1, "text": "x"}]
)
def test_agent_message_rejects_a_malformed_envelope(
    server: ModuleType, agent_cls: MagicMock, message: object
) -> None:
    assert server.invoke({"message": message}) == {
        "error": "message needs 'from' and 'text' strings"
    }
    agent_cls.assert_not_called()
