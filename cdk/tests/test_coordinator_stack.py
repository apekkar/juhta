import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template
from coordinator_stack import COORDINATOR_RUNTIME_PARAMETER, CoordinatorStack
from strategist_stack import StrategistStack
from worker_stack import WorkerStack

ENV = cdk.Environment(account="123456789012", region="eu-west-1")
HAIKU = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
SONNET = "eu.anthropic.claude-sonnet-4-6"


@pytest.fixture(scope="module")
def template() -> Template:
    app = cdk.App()
    strategist = StrategistStack(
        app,
        "Strategist",
        bedrock_model_id=SONNET,
        github_repository="octocat/juhta",
        env=ENV,
    )
    worker = WorkerStack(
        app,
        "Worker",
        bedrock_model_id=SONNET,
        github_repository="octocat/juhta",
        env=ENV,
    )
    coordinator = CoordinatorStack(
        app,
        "Coordinator",
        bedrock_model_id=HAIKU,
        strategist_runtime=strategist.runtime,
        slack_user_id="U123",
        github_repository="octocat/juhta",
        worker_runtime=worker.runtime,
        env=ENV,
    )
    return Template.from_stack(coordinator)


def test_has_its_own_memory_and_secret(template: Template) -> None:
    template.has_resource_properties(
        "AWS::BedrockAgentCore::Memory", {"Name": "JuhtaCoordinatorMemory"}
    )
    template.has_resource_properties("AWS::SecretsManager::Secret", {"Name": "juhta/coordinator"})


def test_runtime_is_configured_for_strategist_and_slack(template: Template) -> None:
    template.has_resource_properties(
        "AWS::BedrockAgentCore::Runtime",
        {
            "AgentRuntimeName": "juhta_coordinator",
            "EnvironmentVariables": Match.object_like(
                {
                    "COORDINATOR_MODEL_ID": HAIKU,
                    "SLACK_USER_ID": "U123",
                    "GITHUB_REPOSITORY": "octocat/juhta",
                    "STRATEGIST_RUNTIME_ARN": Match.any_value(),
                    "AGENT_SECRET_ARN": Match.any_value(),
                    "AGENTCORE_MEMORY_ID": Match.any_value(),
                    "WORKER_RUNTIME_ARN": Match.any_value(),
                    "OTEL_TRACES_SAMPLER": "always_on",
                }
            ),
        },
    )


def test_can_invoke_only_the_strategist_runtime(template: Template) -> None:
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Action": "bedrock-agentcore:InvokeAgentRuntime",
                                "Resource": Match.array_with(
                                    [{"Fn::ImportValue": Match.string_like_regexp("Strategist")}]
                                ),
                            }
                        )
                    ]
                )
            }
        },
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
                                        f"arn:aws:bedrock:eu-west-1:123456789012:inference-profile/{HAIKU}"
                                    ]
                                ),
                            }
                        )
                    ]
                )
            }
        },
    )


def test_coordinator_receiver_lambda_has_a_public_function_url(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "FunctionName": "juhta-coordinator-receiver",
            "Runtime": "python3.14",
            "Architectures": ["arm64"],
            "Handler": "handler.handler",
            "Environment": {
                "Variables": Match.object_like(
                    {
                        "SLACK_USER_ID": "U123",
                        "AGENT_SECRET_ARN": Match.any_value(),
                        "COORDINATOR_RUNTIME_ARN": Match.any_value(),
                    }
                )
            },
        },
    )
    template.has_resource_properties("AWS::Lambda::Url", {"AuthType": "NONE"})
    template.has_output("SlackEventsUrl", {})


def test_coordinator_receiver_can_invoke_only_coordinator_and_itself(template: Template) -> None:
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Action": "lambda:InvokeFunction",
                                "Resource": (
                                    "arn:aws:lambda:eu-west-1:123456789012"
                                    ":function:juhta-coordinator-receiver"
                                ),
                            }
                        ),
                    ]
                )
            }
        },
    )
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Action": "bedrock-agentcore:InvokeAgentRuntime",
                                "Resource": Match.array_with(
                                    [
                                        {
                                            "Fn::GetAtt": [
                                                Match.string_like_regexp("CoordinatorRuntime"),
                                                "AgentRuntimeArn",
                                            ]
                                        }
                                    ]
                                ),
                            }
                        )
                    ]
                )
            }
        },
    )


def test_runs_coordinator_daily_at_seven_helsinki_time_via_the_receiver(
    template: Template,
) -> None:
    template.has_resource_properties(
        "AWS::Scheduler::Schedule",
        {
            "ScheduleExpression": "cron(0 7 * * ? *)",
            "ScheduleExpressionTimezone": "Europe/Helsinki",
            "Target": Match.object_like(
                {
                    "Arn": {"Fn::GetAtt": [Match.string_like_regexp("CoordinatorReceiver"), "Arn"]},
                    "Input": Match.serialized_json({"coordinator_payload": {"action": "daily"}}),
                    "RetryPolicy": Match.object_like({"MaximumRetryAttempts": 0}),
                    # Failed runs land here with the error, instead of vanishing.
                    "DeadLetterConfig": {"Arn": Match.any_value()},
                }
            ),
        },
    )
    template.resource_count_is("AWS::SQS::Queue", 1)


def test_receiver_concurrency_is_capped(template: Template) -> None:
    """Its public Function URL can be flooded; a cap bounds cost."""
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {"FunctionName": "juhta-coordinator-receiver", "ReservedConcurrentExecutions": 100},
    )


def test_dead_letter_queue_rejects_non_tls_access(template: Template) -> None:
    template.has_resource_properties(
        "AWS::SQS::QueuePolicy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Effect": "Deny",
                                "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                            }
                        )
                    ]
                )
            }
        },
    )


def test_can_invoke_the_worker(template: Template) -> None:
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Action": "bedrock-agentcore:InvokeAgentRuntime",
                                "Resource": Match.array_with(
                                    [{"Fn::ImportValue": Match.string_like_regexp("Worker")}]
                                ),
                            }
                        )
                    ]
                )
            }
        },
    )


def test_publishes_its_runtime_arn_for_the_worker(template: Template) -> None:
    template.has_resource_properties(
        "AWS::SSM::Parameter", {"Name": COORDINATOR_RUNTIME_PARAMETER, "Type": "String"}
    )
