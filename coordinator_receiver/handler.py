"""Coordinator's receiver Lambda.

1. Slack request (Function URL): verify, keep only the user's own DMs, hand over async, ack.
2. GitHub webhook (Function URL): verify, turn the few relevant events into an agent message.
3. {"coordinator_payload": {...}} (async self-invoke or schedule): invoke Coordinator's Runtime.
"""

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from dataclasses import dataclass
from functools import cache
from typing import Any

import boto3

MAX_REQUEST_AGE_SECONDS = 300  # Slack's replay-protection window
DEPLOY_WORKFLOW = "on-push-to-main"
WORKER_BRANCH_PREFIX = "worker/"


@dataclass(frozen=True, kw_only=True)
class Settings:
    signing_secret: str
    slack_user_id: str
    coordinator_runtime_arn: str
    region: str
    # GitHub requests are rejected without it.
    github_webhook_secret: str | None


@cache
def settings() -> Settings:
    region = os.environ["AWS_REGION"]
    secret = boto3.client("secretsmanager", region_name=region).get_secret_value(
        SecretId=os.environ["AGENT_SECRET_ARN"]
    )
    secrets = json.loads(secret["SecretString"])
    return Settings(
        signing_secret=secrets["slack_signing_secret"],
        github_webhook_secret=secrets.get("github_webhook_secret"),
        slack_user_id=os.environ["SLACK_USER_ID"],
        coordinator_runtime_arn=os.environ["COORDINATOR_RUNTIME_ARN"],
        region=region,
    )


def handler(event: dict[str, Any], context: Any) -> dict[str, Any] | None:  # noqa: ANN401
    if "requestContext" in event:
        return receive(request=event, function_arn=context.invoked_function_arn)
    invoke_coordinator(event["coordinator_payload"])
    return None


def receive(*, request: dict[str, Any], function_arn: str) -> dict[str, Any]:
    body = request["body"]
    if request["isBase64Encoded"]:
        body = base64.b64decode(body).decode()
    headers = request["headers"]  # Function URLs lowercase header names
    if "x-github-event" in headers:
        return receive_github(body=body, headers=headers, function_arn=function_arn)
    if not is_from_slack(
        body=body,
        timestamp=headers["x-slack-request-timestamp"],
        signature=headers["x-slack-signature"],
    ):
        return {"statusCode": 401}

    payload = json.loads(body)
    if payload["type"] == "url_verification":
        return {"statusCode": 200, "body": payload["challenge"]}
    if "x-slack-retry-num" in headers:
        return {"statusCode": 200}

    message = payload["event"]
    is_my_dm = (
        message["type"] == "message"
        and message.get("channel_type") == "im"
        and "subtype" not in message
        and "bot_id" not in message
        and message.get("user") == settings().slack_user_id
    )
    if is_my_dm:
        hand_over(
            function_arn=function_arn,
            payload={"slack_event": {"channel": message["channel"], "text": message["text"]}},
        )
    return {"statusCode": 200}


def receive_github(*, body: str, headers: dict[str, str], function_arn: str) -> dict[str, Any]:
    if not is_from_github(body=body, signature=headers.get("x-hub-signature-256", "")):
        return {"statusCode": 401}
    text = github_message(event=headers["x-github-event"], payload=json.loads(body))
    if text:
        hand_over(function_arn=function_arn, payload={"message": {"from": "github", "text": text}})
    return {"statusCode": 200}


def github_message(*, event: str, payload: dict[str, Any]) -> str | None:
    """The events Coordinator hears about, as text; None for everything else."""
    if event == "pull_request_review":
        return review_message(payload)
    if event != "workflow_run" or payload.get("action") != "completed":
        return None
    run = payload["workflow_run"]
    result = f"{run['conclusion']}: {run['html_url']}"
    if run["name"] == DEPLOY_WORKFLOW:
        commit = run["head_commit"]["message"].splitlines()[0]
        return f"Deploy of main ({commit}) finished with {result}"
    prs = ", ".join(f"PR #{pr['number']}" for pr in run["pull_requests"]) or "no PR"
    return f"Checks ({run['name']}) on {prs}, branch {run['head_branch']}, finished with {result}"


def review_message(payload: dict[str, Any]) -> str | None:
    """The owner's approval, or their "changes requested" on a Worker PR."""
    review, pr = payload["review"], payload["pull_request"]
    by_owner = review["user"]["login"] == payload["repository"]["owner"]["login"]
    if payload["action"] != "submitted" or not by_owner:
        return None
    if review["state"] == "approved":
        return f"PR #{pr['number']} approved by the user: {review['html_url']}"
    if review["state"] != "changes_requested" or not pr["head"]["ref"].startswith(
        WORKER_BRANCH_PREFIX
    ):
        return None
    return (
        f"Changes requested on PR #{pr['number']} (the Worker's branch {pr['head']['ref']}) "
        f"by the user: {review['html_url']}"
    )


def hand_over(*, function_arn: str, payload: dict[str, Any]) -> None:
    boto3.client("lambda", region_name=settings().region).invoke(
        FunctionName=function_arn,
        InvocationType="Event",
        Payload=json.dumps({"coordinator_payload": payload}),
    )


def is_from_slack(*, body: str, timestamp: str, signature: str) -> bool:
    if abs(time.time() - int(timestamp)) > MAX_REQUEST_AGE_SECONDS:
        return False
    expected = (
        "v0="
        + hmac.new(
            settings().signing_secret.encode(), f"v0:{timestamp}:{body}".encode(), hashlib.sha256
        ).hexdigest()
    )
    return hmac.compare_digest(expected, signature)


def is_from_github(*, body: str, signature: str) -> bool:
    secret = settings().github_webhook_secret
    if not secret:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def invoke_coordinator(payload: dict[str, Any]) -> None:
    # Per-DM session keeps the container warm; AgentCore needs 33+ characters.
    session_id = (
        f"juhta-coordinator-slack-dm-session-{payload['slack_event']['channel']}"
        if "slack_event" in payload
        else str(uuid.uuid4())
    )
    boto3.client("bedrock-agentcore", region_name=settings().region).invoke_agent_runtime(
        agentRuntimeArn=settings().coordinator_runtime_arn,
        runtimeSessionId=session_id,
        payload=json.dumps(payload),
    )["response"].read()
