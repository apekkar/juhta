import aws_cdk as cdk
from aws_cdk.assertions import Match, Template
from strategist_stack import StrategistStack


def test_runtime_can_invoke_the_inference_profile_in_its_own_account() -> None:
    stack = StrategistStack(
        cdk.App(),
        "TestStrategistStack",
        bedrock_model_id="eu.anthropic.claude-sonnet-4-6",
        github_repository="octocat/juhta",
        env=cdk.Environment(account="123456789012", region="eu-west-1"),
    )

    Template.from_stack(stack).has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Action": Match.array_with(
                                    ["bedrock:InvokeModelWithResponseStream"]
                                ),
                                "Resource": Match.array_with(
                                    [
                                        "arn:aws:bedrock:eu-west-1:123456789012"
                                        ":inference-profile/eu.anthropic.claude-sonnet-4-6"
                                    ]
                                ),
                            }
                        )
                    ]
                )
            }
        },
    )


def test_runtime_delivers_traces() -> None:
    stack = StrategistStack(
        cdk.App(),
        "TestStrategistStack",
        bedrock_model_id="eu.anthropic.claude-sonnet-4-6",
        github_repository="octocat/juhta",
        env=cdk.Environment(account="123456789012", region="eu-west-1"),
    )

    template = Template.from_stack(stack)
    template.resource_count_is("AWS::Logs::Delivery", 1)
    template.has_resource_properties(
        "AWS::BedrockAgentCore::Runtime",
        {"EnvironmentVariables": Match.object_like({"OTEL_TRACES_SAMPLER": "always_on"})},
    )


def test_runtime_can_read_only_the_github_token_secret() -> None:
    stack = StrategistStack(
        cdk.App(),
        "TestStrategistStack",
        bedrock_model_id="eu.anthropic.claude-sonnet-4-6",
        github_repository="octocat/juhta",
        env=cdk.Environment(account="123456789012", region="eu-west-1"),
    )
    template = Template.from_stack(stack)

    template.has_resource_properties("AWS::SecretsManager::Secret", {"Name": "juhta/strategist"})
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Action": Match.array_with(["secretsmanager:GetSecretValue"]),
                                "Resource": {"Ref": Match.string_like_regexp("AgentSecret")},
                            }
                        )
                    ]
                )
            }
        },
    )
    template.has_resource_properties(
        "AWS::BedrockAgentCore::Runtime",
        {
            "EnvironmentVariables": Match.object_like(
                {
                    "GITHUB_REPOSITORY": "octocat/juhta",
                    "AGENT_SECRET_ARN": {"Ref": Match.string_like_regexp("AgentSecret")},
                }
            )
        },
    )


def test_has_no_schedule_of_its_own() -> None:
    """The daily run is Coordinator's (it calls Strategist, then DMs the user)."""
    stack = StrategistStack(
        cdk.App(),
        "TestStrategistStack",
        bedrock_model_id="eu.anthropic.claude-sonnet-4-6",
        github_repository="octocat/juhta",
        env=cdk.Environment(account="123456789012", region="eu-west-1"),
    )

    Template.from_stack(stack).resource_count_is("AWS::Scheduler::Schedule", 0)
