"""CDK stack: the Worker's secret and AgentCore Runtime (no Memory: one run per issue).

Coordinator's stack depends on this one, so the Worker finds Coordinator by its SSM
parameter and runtime name, not a cross-stack reference.
"""

from pathlib import Path

from aws_cdk import CfnOutput, Duration, Environment, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct
from coordinator_stack import COORDINATOR_RUNTIME_PARAMETER
from nag import (
    CROSS_REGION_MODELS,
    NO_AUTOMATIC_ROTATION,
    RUNTIME_DEFAULT_ROLE_REASON,
    account_in_findings,
    acknowledge,
    runtime_default_role_findings,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class WorkerStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        bedrock_model_id: str,
        github_repository: str,
        env: Environment,
    ) -> None:
        super().__init__(scope, construct_id, env=env)

        agent_secret = secretsmanager.Secret(
            self,
            "AgentSecret",
            secret_name="juhta/worker",  # noqa: S106 - a name, not a password
            description='Worker secrets as JSON: {"github_pat": "<Contents + PRs write PAT>"}',
        )

        self.runtime = agentcore.Runtime(
            self,
            "WorkerRuntime",
            runtime_name="juhta_worker",
            description="Juhta Worker: implements issues and opens draft PRs.",
            agent_runtime_artifact=agentcore.AgentRuntimeArtifact.from_asset(
                str(REPO_ROOT),
                file="worker/Dockerfile",
                platform=ecr_assets.Platform.LINUX_ARM64,
            ),
            network_configuration=agentcore.RuntimeNetworkConfiguration.using_public_network(),
            protocol_configuration=agentcore.ProtocolType.HTTP,
            environment_variables={
                "AWS_REGION": self.region,
                "WORKER_MODEL_ID": bedrock_model_id,
                "GITHUB_REPOSITORY": github_repository,
                "AGENT_SECRET_ARN": agent_secret.secret_arn,
                "COORDINATOR_RUNTIME_PARAMETER": COORDINATOR_RUNTIME_PARAMETER,
                "OTEL_TRACES_SAMPLER": "always_on",
            },
            lifecycle_configuration=agentcore.LifecycleConfiguration(
                idle_runtime_session_timeout=Duration.minutes(15),
                max_lifetime=Duration.hours(2),
            ),
            tracing_enabled=True,
        )
        agent_secret.grant_read(self.runtime)
        coordinator = (
            f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:runtime/juhta_coordinator-*"
        )
        self.runtime.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=[coordinator, f"{coordinator}/*"],
            )
        )
        self.runtime.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[
                    f"arn:aws:ssm:{self.region}:{self.account}"
                    f":parameter{COORDINATOR_RUNTIME_PARAMETER}"
                ],
            )
        )

        model_access = iam.Policy(
            self,
            "WorkerModelAccess",
            roles=[self.runtime.role],
            statements=[
                iam.PolicyStatement(
                    actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                    resources=[
                        f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/{bedrock_model_id}",
                        "arn:aws:bedrock:*::foundation-model/anthropic.*",
                    ],
                ),
            ],
        )

        acknowledge(
            self.runtime,
            {
                **{
                    f"AwsSolutions-IAM5[{finding}]": RUNTIME_DEFAULT_ROLE_REASON
                    for finding in runtime_default_role_findings(self)
                },
                **{
                    f"AwsSolutions-IAM5[Resource::arn:aws:bedrock-agentcore:{self.region}:"
                    f"{account_in_findings(self)}:runtime/juhta_coordinator-*{suffix}]": (
                        "Invoke Coordinator, to report: its ARN ends in a random suffix, and a "
                        "stack reference would be a cycle (Coordinator invokes the Worker)."
                    )
                    for suffix in ("", "/*")
                },
            },
        )
        acknowledge(model_access, CROSS_REGION_MODELS)
        acknowledge(agent_secret, NO_AUTOMATIC_ROTATION)

        CfnOutput(self, "WorkerRuntimeArn", value=self.runtime.agent_runtime_arn)
