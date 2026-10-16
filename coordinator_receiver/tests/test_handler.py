import hashlib
import hmac
import io
import json
import time
from collections.abc import Iterator
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

from coordinator_receiver import handler

SIGNING_SECRET = "test-signing-secret"
GITHUB_SECRET = "test-github-webhook-secret"
FUNCTION_ARN = "arn:aws:lambda:eu-west-1:123:function:juhta-coordinator-receiver"
SETTINGS = handler.Settings(
    signing_secret=SIGNING_SECRET,
    slack_user_id="U123",
    coordinator_runtime_arn="arn:aws:bedrock-agentcore:eu-west-1:123:runtime/juhta_coordinator-x",
    region="eu-west-1",
    github_webhook_secret=GITHUB_SECRET,
)
AGENTCORE_MIN_SESSION_ID_LENGTH = 33


def slack_request(
    payload: dict[str, object], *, secret: str = SIGNING_SECRET, age: int = 0, retry: bool = False
) -> dict[str, object]:
    """A Lambda Function URL event as Slack would send it."""
    body = json.dumps(payload)
    timestamp = str(int(time.time()) - age)
    signature = (
        "v0="
        + hmac.new(secret.encode(), f"v0:{timestamp}:{body}".encode(), hashlib.sha256).hexdigest()
    )
    headers = {"x-slack-request-timestamp": timestamp, "x-slack-signature": signature}
    if retry:
        headers["x-slack-retry-num"] = "1"
    return {
        "requestContext": {"http": {"method": "POST", "path": "/"}},
        "body": body,
        "isBase64Encoded": False,
        "headers": headers,
    }


def dm(**overrides: object) -> dict[str, object]:
    event = {"type": "message", "channel_type": "im", "channel": "D999", "user": "U123"}
    event["text"] = "What should we build next?"
    return {"type": "event_callback", "event": {**event, **overrides}}


@pytest.fixture
def aws() -> Iterator[MagicMock]:
    with (
        patch.object(handler, "settings", return_value=SETTINGS),
        patch.object(handler, "boto3") as boto3,
    ):
        yield boto3.client.return_value


def context() -> MagicMock:
    return MagicMock(invoked_function_arn=FUNCTION_ARN)


def test_answers_slacks_url_verification(aws: MagicMock) -> None:
    request = slack_request({"type": "url_verification", "challenge": "abc"})

    assert handler.handler(request, context()) == {"statusCode": 200, "body": "abc"}


def test_rejects_a_bad_signature(aws: MagicMock) -> None:
    response = handler.handler(slack_request(dm(), secret="wrong"), context())

    assert response == {"statusCode": 401}
    aws.invoke.assert_not_called()


def test_rejects_an_old_request(aws: MagicMock) -> None:
    response = handler.handler(slack_request(dm(), age=600), context())

    assert response == {"statusCode": 401}


def test_hands_my_dm_to_the_async_mode_and_acks(aws: MagicMock) -> None:
    response = handler.handler(slack_request(dm()), context())

    assert response == {"statusCode": 200}
    aws.invoke.assert_called_once()
    call = aws.invoke.call_args.kwargs
    assert call["FunctionName"] == FUNCTION_ARN
    assert call["InvocationType"] == "Event"
    assert json.loads(call["Payload"]) == {
        "coordinator_payload": {
            "slack_event": {"channel": "D999", "text": "What should we build next?"}
        }
    }


@pytest.mark.parametrize(
    "event",
    [
        dm(user="U_SOMEONE_ELSE"),
        dm(bot_id="B1"),  # the bot's own reply: must not loop
        dm(subtype="message_changed"),
        dm(channel_type="channel"),
    ],
)
def test_ignores_everything_but_my_dms(aws: MagicMock, event: dict[str, object]) -> None:
    assert handler.handler(slack_request(event), context()) == {"statusCode": 200}
    aws.invoke.assert_not_called()


def test_ignores_slack_retries(aws: MagicMock) -> None:
    assert handler.handler(slack_request(dm(), retry=True), context()) == {"statusCode": 200}
    aws.invoke.assert_not_called()


def test_async_mode_invokes_coordinator_for_the_dm(aws: MagicMock) -> None:
    aws.invoke_agent_runtime.return_value = {"response": io.BytesIO(b'{"reply": "ok"}')}

    handler.handler(
        {"coordinator_payload": {"slack_event": {"channel": "D999", "text": "Hi"}}}, context()
    )

    call = aws.invoke_agent_runtime.call_args.kwargs
    assert call["agentRuntimeArn"] == SETTINGS.coordinator_runtime_arn
    assert json.loads(call["payload"]) == {"slack_event": {"channel": "D999", "text": "Hi"}}
    assert len(call["runtimeSessionId"]) >= AGENTCORE_MIN_SESSION_ID_LENGTH
    assert "D999" in call["runtimeSessionId"]  # same DM → same warm Runtime session


def test_forwards_the_daily_schedule_payload_to_coordinator(aws: MagicMock) -> None:
    aws.invoke_agent_runtime.return_value = {"response": io.BytesIO(b'{"design": {}}')}

    handler.handler({"coordinator_payload": {"action": "daily"}}, context())

    call = aws.invoke_agent_runtime.call_args.kwargs
    assert json.loads(call["payload"]) == {"action": "daily"}
    assert len(call["runtimeSessionId"]) >= AGENTCORE_MIN_SESSION_ID_LENGTH


def test_a_function_url_request_is_always_signature_checked(aws: MagicMock) -> None:
    request = {**slack_request(dm(), secret="wrong"), "coordinator_payload": {"action": "daily"}}

    assert handler.handler(request, context()) == {"statusCode": 401}
    aws.invoke_agent_runtime.assert_not_called()
    aws.invoke.assert_not_called()


def github_request(
    payload: dict[str, object], *, event: str = "workflow_run", secret: str = GITHUB_SECRET
) -> dict[str, object]:
    """A Lambda Function URL event as a GitHub repository webhook would send it."""
    body = json.dumps(payload)
    signature = "sha256=" + hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return {
        "requestContext": {"http": {"method": "POST", "path": "/"}},
        "body": body,
        "isBase64Encoded": False,
        "headers": {"x-github-event": event, "x-hub-signature-256": signature},
    }


def workflow_run(**overrides: object) -> dict[str, object]:
    run = {
        "name": "on-pull-request",
        "conclusion": "failure",
        "head_branch": "worker/issue-41-1",
        "html_url": "https://github.com/octocat/juhta/actions/runs/1",
        "pull_requests": [{"number": 41}],
        "head_commit": {"message": "Add sign-out\n\nbody"},
    }
    return {"action": "completed", "workflow_run": {**run, **overrides}}


def forwarded_text(aws: MagicMock) -> str:
    payload = json.loads(aws.invoke.call_args.kwargs["Payload"])["coordinator_payload"]
    assert payload["message"]["from"] == "github"
    text: str = payload["message"]["text"]
    return text


def test_rejects_a_github_request_with_a_bad_signature(aws: MagicMock) -> None:
    response = handler.handler(github_request(workflow_run(), secret="wrong"), context())

    assert response == {"statusCode": 401}
    aws.invoke.assert_not_called()


def test_rejects_github_requests_until_the_webhook_secret_is_set(aws: MagicMock) -> None:
    with patch.object(
        handler, "settings", return_value=replace(SETTINGS, github_webhook_secret=None)
    ):
        response = handler.handler(github_request(workflow_run()), context())

    assert response == {"statusCode": 401}


def test_answers_githubs_ping(aws: MagicMock) -> None:
    response = handler.handler(github_request({"zen": "Keep it simple."}, event="ping"), context())

    assert response == {"statusCode": 200}
    aws.invoke.assert_not_called()


def test_tells_coordinator_when_a_prs_checks_finish(aws: MagicMock) -> None:
    response = handler.handler(github_request(workflow_run()), context())

    assert response == {"statusCode": 200}
    text = forwarded_text(aws)
    for part in ("PR #41", "failure", "worker/issue-41-1", "actions/runs/1"):
        assert part in text


def test_tells_coordinator_how_a_deploy_went(aws: MagicMock) -> None:
    run = workflow_run(name="on-push-to-main", conclusion="success", pull_requests=[])

    handler.handler(github_request(run), context())

    text = forwarded_text(aws)
    assert "Deploy" in text
    assert "success" in text
    assert "Add sign-out" in text


@pytest.mark.parametrize(
    ("event", "payload"),
    [
        ("workflow_run", {"action": "requested", "workflow_run": {}}),
        ("pull_request", {"action": "opened"}),
    ],
)
def test_ignores_other_github_events(
    aws: MagicMock, event: str, payload: dict[str, object]
) -> None:
    assert handler.handler(github_request(payload, event=event), context()) == {"statusCode": 200}
    aws.invoke.assert_not_called()


def review(**overrides: object) -> dict[str, object]:
    """A pull_request_review webhook payload: the owner asks for changes on a Worker PR."""
    payload: dict[str, object] = {
        "action": "submitted",
        "review": {
            "state": "changes_requested",
            "user": {"login": "octocat"},
            "html_url": "https://github.com/octocat/juhta/pull/47#pullrequestreview-1",
        },
        "pull_request": {"number": 47, "head": {"ref": "worker/issue-46-1"}},
        "repository": {"owner": {"login": "octocat"}},
    }
    return {**payload, **overrides}


def test_tells_coordinator_when_the_owner_asks_for_changes_on_a_worker_pr(aws: MagicMock) -> None:
    handler.handler(github_request(review(), event="pull_request_review"), context())

    text = forwarded_text(aws)
    for part in ("Changes requested on PR #47", "worker/issue-46-1", "pullrequestreview-1"):
        assert part in text


@pytest.mark.parametrize(
    "payload",
    [
        review(review={"state": "changes_requested", "user": {"login": "someone"}, "html_url": ""}),
        review(pull_request={"number": 9, "head": {"ref": "feature/mine"}}),
        review(action="edited"),
    ],
)
def test_ignores_other_reviews(aws: MagicMock, payload: dict[str, object]) -> None:
    handler.handler(github_request(payload, event="pull_request_review"), context())

    aws.invoke.assert_not_called()


def test_tells_coordinator_when_the_owner_approves_a_pr(aws: MagicMock) -> None:
    approved = review(
        review={"state": "approved", "user": {"login": "octocat"}, "html_url": "https://x/r"},
        pull_request={"number": 9, "head": {"ref": "feature/mine"}},
    )

    handler.handler(github_request(approved, event="pull_request_review"), context())

    text = forwarded_text(aws)
    assert "PR #9 approved by the user" in text
