from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template
from coordinator_stack import CoordinatorStack
from dashboard_stack import DashboardStack
from strategist_stack import StrategistStack
from worker_stack import WorkerStack

ENV = cdk.Environment(account="123456789012", region="eu-west-1")


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> Template:
    dist = tmp_path_factory.mktemp("dist")
    (dist / "index.html").write_text("<html></html>")
    app = cdk.App()
    strategist = StrategistStack(
        app,
        "Strategist",
        bedrock_model_id="eu.anthropic.claude-sonnet-4-6",
        github_repository="octocat/juhta",
        env=ENV,
    )
    worker = WorkerStack(
        app,
        "Worker",
        bedrock_model_id="eu.anthropic.claude-sonnet-4-6",
        github_repository="octocat/juhta",
        env=ENV,
    )

    coordinator = CoordinatorStack(
        app,
        "Coordinator",
        bedrock_model_id="eu.anthropic.claude-haiku-4-5-20251001-v1:0",
        strategist_runtime=strategist.runtime,
        slack_user_id="U123",
        github_repository="octocat/juhta",
        worker_runtime=worker.runtime,
        env=ENV,
    )
    dashboard = DashboardStack(
        app,
        "Dashboard",
        memory=strategist.memory,
        runtime=strategist.runtime,
        coordinator_runtime=coordinator.runtime,
        worker_runtime=worker.runtime,
        dist_path=str(Path(dist)),
        env=ENV,
    )
    return Template.from_stack(dashboard)


def test_only_admins_create_users(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Cognito::UserPool",
        {"AdminCreateUserConfig": {"AllowAdminCreateUserOnly": True}},
    )


def test_identity_pool_rejects_unauthenticated(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Cognito::IdentityPool", {"AllowUnauthenticatedIdentities": False}
    )


def test_signed_in_user_can_read_memory_and_invoke_runtime(template: Template) -> None:
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {"Action": Match.array_with(["bedrock-agentcore:ListEvents"])}
                        ),
                        Match.object_like({"Action": "bedrock-agentcore:InvokeAgentRuntime"}),
                    ]
                )
            }
        },
    )


def test_bucket_is_private(template: Template) -> None:
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            }
        },
    )


def test_cloudfront_reads_bucket_via_origin_access_control(template: Template) -> None:
    template.resource_count_is("AWS::CloudFront::OriginAccessControl", 1)


def test_signed_in_user_can_query_only_the_agents_spans(template: Template) -> None:
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Action": "logs:StartQuery",
                                "Resource": "arn:aws:logs:eu-west-1:123456789012"
                                ":log-group:aws/spans:*",
                            }
                        ),
                        Match.object_like({"Action": "logs:GetQueryResults", "Resource": "*"}),
                    ]
                )
            }
        },
    )


def test_signed_in_user_has_no_cloudwatch_metrics_access(template: Template) -> None:
    policies = template.find_resources("AWS::IAM::Policy")
    assert "cloudwatch:GetMetricData" not in str(policies)


def test_user_pool_requires_strong_passwords(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Cognito::UserPool",
        {
            "Policies": {
                "PasswordPolicy": Match.object_like(
                    {
                        "MinimumLength": 14,
                        "RequireLowercase": True,
                        "RequireUppercase": True,
                        "RequireNumbers": True,
                        "RequireSymbols": True,
                    }
                )
            }
        },
    )


def test_signed_in_user_can_ask_coordinator_to_file_issues(template: Template) -> None:
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
                                    [{"Fn::ImportValue": Match.string_like_regexp("Coordinator")}]
                                ),
                            }
                        )
                    ]
                )
            }
        },
    )


def test_web_client_signs_in_with_srp_and_keeps_sessions_one_day(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Cognito::UserPoolClient",
        {
            "ExplicitAuthFlows": ["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
            "RefreshTokenValidity": 1440,
            "TokenValidityUnits": Match.object_like({"RefreshToken": "minutes"}),
        },
    )


def test_browsers_revalidate_the_site_on_every_load(template: Template) -> None:
    template.has_resource_properties(
        "AWS::CloudFront::ResponseHeadersPolicy",
        {
            "ResponseHeadersPolicyConfig": Match.object_like(
                {
                    "CustomHeadersConfig": {
                        "Items": [
                            {"Header": "Cache-Control", "Value": "no-cache", "Override": True}
                        ]
                    }
                }
            )
        },
    )


def test_signed_in_user_can_ping_the_worker(template: Template) -> None:
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
