import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template
from coordinator_stack import COORDINATOR_RUNTIME_PARAMETER
from worker_stack import WorkerStack

ENV = cdk.Environment(account="123456789012", region="eu-west-1")
SONNET = "eu.anthropic.claude-sonnet-4-6"


@pytest.fixture(scope="module")
def template() -> Template:
    app = cdk.App()
    worker = WorkerStack(
        app,
        "Worker",
        bedrock_model_id=SONNET,
        github_repository="octocat/juhta",
        env=ENV,
    )
    return Template.from_stack(worker)


def test_has_its_own_secret(template: Template) -> None:
    template.has_resource_properties("AWS::SecretsManager::Secret", {"Name": "juhta/worker"})


def test_runtime_is_configured_for_its_model_repo_and_coordinator(template: Template) -> None:
    template.has_resource_properties(
        "AWS::BedrockAgentCore::Runtime",
        {
            "AgentRuntimeName": "juhta_worker",
            "EnvironmentVariables": Match.object_like(
                {
                    "WORKER_MODEL_ID": SONNET,
                    "GITHUB_REPOSITORY": "octocat/juhta",
                    "AGENT_SECRET_ARN": Match.any_value(),
                    "COORDINATOR_RUNTIME_PARAMETER": COORDINATOR_RUNTIME_PARAMETER,
                    "OTEL_TRACES_SAMPLER": "always_on",
                }
            ),
        },
    )


def test_sessions_last_long_enough_for_an_issue(template: Template) -> None:
    template.has_resource_properties(
        "AWS::BedrockAgentCore::Runtime",
        {"LifecycleConfiguration": {"IdleRuntimeSessionTimeout": 900, "MaxLifetime": 7200}},
    )


def test_can_invoke_its_model(template: Template) -> None:
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Action": Match.array_with(["bedrock:InvokeModel"]),
                                "Resource": Match.array_with(
                                    [
                                        "arn:aws:bedrock:eu-west-1:123456789012"
                                        f":inference-profile/{SONNET}"
                                    ]
                                ),
                            }
                        )
                    ]
                )
            }
        },
    )


def test_finds_and_may_invoke_only_coordinator_without_a_stack_dependency(
    template: Template,
) -> None:
    # No cross-stack reference: Coordinator's stack depends on this one.
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Action": "bedrock-agentcore:InvokeAgentRuntime",
                                "Resource": [
                                    "arn:aws:bedrock-agentcore:eu-west-1:123456789012"
                                    ":runtime/juhta_coordinator-*",
                                    "arn:aws:bedrock-agentcore:eu-west-1:123456789012"
                                    ":runtime/juhta_coordinator-*/*",
                                ],
                            }
                        ),
                        Match.object_like(
                            {
                                "Action": "ssm:GetParameter",
                                "Resource": "arn:aws:ssm:eu-west-1:123456789012:parameter"
                                f"{COORDINATOR_RUNTIME_PARAMETER}",
                            }
                        ),
                    ]
                )
            }
        },
    )
    assert "Fn::ImportValue" not in str(template.to_json())
