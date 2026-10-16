"""Fakes for the model, GitHub's MCP server and AgentCore."""

import io
import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

from strands.models import Model
from strands.types.content import ContentBlockStartToolUse
from strands.types.streaming import StreamEvent


class ScriptedModel(Model):
    """Answers each model turn from a script: a tool call {"tool": name, "input": {...}} or text."""

    def __init__(self, *, turns: list[dict[str, Any] | str]) -> None:
        self.turns = list(turns)

    def update_config(self, **model_config: Any) -> None:  # noqa: ANN401
        pass

    def get_config(self) -> dict[str, Any]:
        return {}

    def structured_output(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        raise NotImplementedError

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[StreamEvent]:  # noqa: ANN401
        turn = self.turns.pop(0)
        yield {"messageStart": {"role": "assistant"}}
        if isinstance(turn, str):
            yield {"contentBlockDelta": {"delta": {"text": turn}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
            return
        tool_use: ContentBlockStartToolUse = {
            "toolUseId": f"t{len(self.turns)}",
            "name": turn["tool"],
        }
        yield {"contentBlockStart": {"start": {"toolUse": tool_use}}}
        yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(turn["input"])}}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use"}}


def github_mcp_answering(answers: dict[str, Any]) -> MagicMock:
    """Tools return `answers[tool]` as JSON; an answer may be a function of the arguments."""

    def answer(**call: Any) -> dict[str, Any]:  # noqa: ANN401
        result = answers[call["name"]]
        if callable(result):
            result = result(call["arguments"])
        return {"status": "success", "content": [{"text": json.dumps(result)}]}

    github = MagicMock()
    github.__enter__.return_value = github
    github.call_tool_sync.side_effect = answer
    return github


def agentcore_answering(answer: dict[str, str]) -> MagicMock:
    agentcore = MagicMock()
    agentcore.invoke_agent_runtime.side_effect = lambda **_: {
        "response": io.BytesIO(json.dumps(answer).encode())
    }
    return agentcore
