import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template
from github_stack import GithubStack

REPO_SUB = "repo:octocat@1111/juhta@2222"
MODELS = ["eu.anthropic.claude-sonnet-4-6", "eu.anthropic.claude-haiku-4-5-20251001-v1:0"]


@pytest.fixture(scope="module")
def template() -> Template:
    stack = GithubStack(
        cdk.App(),
        "TestGithubStack",
        github_repository="octocat/juhta",
        github_repository_owner_id="1111",
        github_repository_id="2222",
        bedrock_tests_model_id=MODELS[0],
        env=cdk.Environment(account="123456789012", region="eu-west-1"),
    )
    return Template.from_stack(stack)


def trust(*, role_name: str, sub: object) -> dict[str, object]:
    return {
        "RoleName": role_name,
        "AssumeRolePolicyDocument": Match.object_like(
            {
                "Statement": [
                    Match.object_like(
                        {
                            "Condition": {
                                "StringEquals": {
                                    "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                                    "token.actions.githubusercontent.com:sub": sub,
                                }
                            }
                        }
                    )
                ]
            }
        ),
    }


def test_deploy_role_trusts_main_and_the_production_environment(template: Template) -> None:
    template.has_resource_properties(
        "AWS::IAM::Role",
        trust(
            role_name="JuhtaGithubDeployRole",
            sub=[f"{REPO_SUB}:ref:refs/heads/main", f"{REPO_SUB}:environment:production"],
        ),
    )


def test_bedrock_tests_role_trusts_only_its_environment(template: Template) -> None:
    template.has_resource_properties(
        "AWS::IAM::Role",
        trust(role_name="JuhtaGithubBedrockTestsRole", sub=f"{REPO_SUB}:environment:bedrock-tests"),
    )


def test_bedrock_tests_role_can_only_invoke_its_model(template: Template) -> None:
    policies = template.find_resources("AWS::IAM::Policy")
    (role,) = [p for name, p in policies.items() if name.startswith("GithubBedrockTestsRole")]
    statements = role["Properties"]["PolicyDocument"]["Statement"]
    assert [s["Action"] for s in statements] == [
        ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    ]
    assert (
        f"arn:aws:bedrock:eu-west-1:123456789012:inference-profile/{MODELS[0]}"
        in statements[0]["Resource"]
    )
