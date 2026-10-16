"""AgentCore Runtime entrypoint for Strategist (/invocations and /ping)."""

from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from .agent import DAILY_PROMPT, StrategistAgent

app = BedrockAgentCoreApp()
strategist = StrategistAgent.from_env()


@app.entrypoint
def invoke(payload: dict[str, Any]) -> dict[str, Any]:
    """Route by payload: action ping, plan, designs or daily; else design for "prompt"."""
    if payload.get("action") == "ping":
        return {"status": "ok"}
    if payload.get("action") == "plan":
        return {"plan": strategist.read_plan()}
    if payload.get("action") == "designs":
        return {"designs": strategist.recent_designs(limit=int(payload["limit"]))}
    if payload.get("action") == "daily":
        return {
            "design": strategist.design(
                workflow_description=DAILY_PROMPT, source="daily"
            ).model_dump()
        }

    description = payload.get("prompt", "")
    if not description:
        return {"error": "Missing 'prompt' in request payload"}

    return {
        "design": strategist.design(
            workflow_description=description, source="on-demand"
        ).model_dump()
    }


if __name__ == "__main__":
    app.run()
