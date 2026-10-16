import importlib
import sys
from collections.abc import Iterator
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from strategist.agent import DAILY_PROMPT, AgentDesign


@pytest.fixture
def agent() -> MagicMock:
    return MagicMock()


@pytest.fixture
def server(agent: MagicMock) -> Iterator[ModuleType]:
    sys.modules.pop("strategist.server", None)
    with patch("strategist.agent.StrategistAgent.from_env", return_value=agent):
        yield importlib.import_module("strategist.server")
    sys.modules.pop("strategist.server", None)


def test_plan_action_returns_the_plan_without_running_the_agent(
    server: ModuleType, agent: MagicMock
) -> None:
    agent.read_plan.return_value = "# Plan"

    assert server.invoke({"action": "plan"}) == {"plan": "# Plan"}
    agent.design.assert_not_called()


def test_prompt_returns_a_design(server: ModuleType, agent: MagicMock) -> None:
    design = AgentDesign(
        name="InboxTriage",
        role="classifier",
        description="Sorts incoming email",
        system_prompt="You triage email.",
        capabilities=["classify"],
        implementation_effort="low",
        plan_step="Outside the plan",
    )
    agent.design.return_value = design

    assert server.invoke({"prompt": "Email triage"}) == {"design": design.model_dump()}
    agent.design.assert_called_once_with(workflow_description="Email triage", source="on-demand")


def test_daily_action_designs_with_the_self_directed_prompt(
    server: ModuleType, agent: MagicMock
) -> None:
    agent.design.return_value.model_dump.return_value = {"name": "Worker"}

    assert server.invoke({"action": "daily"}) == {"design": {"name": "Worker"}}
    agent.design.assert_called_once_with(workflow_description=DAILY_PROMPT, source="daily")


def test_designs_action_returns_recent_designs_without_running_the_agent(
    server: ModuleType, agent: MagicMock
) -> None:
    agent.recent_designs.return_value = [{"source": "daily"}]

    assert server.invoke({"action": "designs", "limit": 2}) == {"designs": [{"source": "daily"}]}
    agent.recent_designs.assert_called_once_with(limit=2)
    agent.design.assert_not_called()


def test_ping_action_answers_without_running_the_agent(
    server: ModuleType, agent: MagicMock
) -> None:
    assert server.invoke({"action": "ping"}) == {"status": "ok"}
    agent.design.assert_not_called()
