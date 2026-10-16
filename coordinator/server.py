"""AgentCore Runtime entrypoint for Coordinator.

Payloads: {"prompt", "session_id"} (CLI), {"slack_event"}, {"message": {"from", "text"}}
(another agent, read-only turn), {"action": "daily"}, {"action": "ping"} (health check).
"""

from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from . import agent

app = BedrockAgentCoreApp()
settings = agent.CoordinatorSettings.from_env()


@app.entrypoint
def invoke(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("action") == "ping":
        return {"status": "ok"}
    if payload.get("action") == "daily":
        return {"design": agent.run_daily(settings=settings)}

    slack_session_id = agent.SLACK_SESSION_ID.format(user_id=settings.slack_user_id)
    if "message" in payload:
        message = payload["message"]
        if not (
            isinstance(message, dict)
            and isinstance(message.get("from"), str)
            and isinstance(message.get("text"), str)
        ):
            return {"error": "message needs 'from' and 'text' strings"}
        coordinator = agent.CoordinatorAgent(
            settings=settings, session_id=slack_session_id, read_only=True
        )
        reply = str(
            coordinator(
                agent.AGENT_MESSAGE_PROMPT.format(sender=message["from"], text=message["text"])
            )
        )
        agent.post_slack_message(
            token=settings.slack_bot_token, channel=settings.slack_user_id, text=reply
        )
        return {"reply": reply}

    if "slack_event" in payload:
        channel = payload["slack_event"]["channel"]
        coordinator = agent.CoordinatorAgent(settings=settings, session_id=slack_session_id)
        reply = str(coordinator(payload["slack_event"]["text"]))
        agent.post_slack_message(token=settings.slack_bot_token, channel=channel, text=reply)
        return {"reply": reply}

    coordinator = agent.CoordinatorAgent(settings=settings, session_id=payload["session_id"])
    return {"reply": str(coordinator(payload["prompt"]))}


if __name__ == "__main__":
    app.run()
