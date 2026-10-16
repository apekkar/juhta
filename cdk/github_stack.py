"""CDK stack: GitHub Actions access to AWS via OIDC.

A deploy role that may only assume the CDK bootstrap roles, and a test role that may only
invoke the Worker's model.
"""

from aws_cdk import CfnOutput, Environment, Stack
from aws_cdk import aws_iam as iam
from constructs import Construct
from nag import CROSS_REGION_MODELS, account_in_findings, acknowledge


class GithubStack(Stack):
    def __init__(  # noqa: PLR0913 - keyword-only config
        self,
        scope: Construct,
        construct_id: str,
        *,
        github_repository: str,
        github_repository_owner_id: str,
        github_repository_id: str,
        bedrock_tests_model_id: str,
        env: Environment,
    ) -> None:
        super().__init__(scope, construct_id, env=env)
        owner, repo = github_repository.split("/")

        provider = iam.OpenIdConnectProvider(
            self,
            "GithubOidcProvider",
            url="https://token.actions.githubusercontent.com",
            client_ids=["sts.amazonaws.com"],
        )

        repo_sub = f"repo:{owner}@{github_repository_owner_id}/{repo}@{github_repository_id}"

        def github(sub: str | list[str]) -> iam.OpenIdConnectPrincipal:
            return iam.OpenIdConnectPrincipal(
                provider,
                conditions={
                    "StringEquals": {
                        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                        "token.actions.githubusercontent.com:sub": sub,
                    }
                },
            )

        role = iam.Role(
            self,
            "GithubDeployRole",
            role_name="JuhtaGithubDeployRole",
            assumed_by=github(
                [f"{repo_sub}:ref:refs/heads/main", f"{repo_sub}:environment:production"]
            ),
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["sts:AssumeRole"],
                resources=[f"arn:aws:iam::{self.account}:role/cdk-hnb659fds-*-{self.region}"],
            )
        )

        bedrock_tests_role = iam.Role(
            self,
            "GithubBedrockTestsRole",
            role_name="JuhtaGithubBedrockTestsRole",
            assumed_by=github(f"{repo_sub}:environment:bedrock-tests"),
        )
        bedrock_tests_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=[
                    f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/{bedrock_tests_model_id}",
                    "arn:aws:bedrock:*::foundation-model/anthropic.*",
                ],
            )
        )

        acknowledge(
            role,
            {
                f"AwsSolutions-IAM5[Resource::arn:aws:iam::{account_in_findings(self)}"
                f":role/cdk-hnb659fds-*-{self.region}]": (
                    "Deploys assume the CDK bootstrap roles (cdk-hnb659fds-*), nothing else."
                ),
            },
        )
        acknowledge(bedrock_tests_role, CROSS_REGION_MODELS)

        CfnOutput(self, "GithubDeployRoleArn", value=role.role_arn)
        CfnOutput(self, "GithubBedrockTestsRoleArn", value=bedrock_tests_role.role_arn)
