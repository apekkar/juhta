"""CDK stack: Strategist's AgentCore Memory and AgentCore Runtime.

Memory is retained on `cdk destroy`: it holds the design history. The Runtime keeps its
auto-created execution role (ECR pull, logging); Bedrock and Memory access are added on top.
"""

from pathlib import Path

from aws_cdk import CfnOutput, Duration, Environment, RemovalPolicies, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct
from nag import (
    CROSS_REGION_MODELS,
    NO_AUTOMATIC_ROTATION,
    RUNTIME_DEFAULT_ROLE_REASON,
    acknowledge,
    runtime_default_role_findings,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class StrategistStack(Stack):
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

        # Values are set by hand, so they never enter a template.
        agent_secret = secretsmanager.Secret(
            self,
            "AgentSecret",
            secret_name="juhta/strategist",  # noqa: S106 - a name, not a password
            description='Strategist secrets as JSON: {"github_pat": "<read-only PAT>"}',
        )

        self.memory = agentcore.Memory(
            self,
            "StrategistMemory",
            memory_name="JuhtaStrategistMemory",
            description="Strategist conversation and design history (short-term events only).",
            expiration_duration=Duration.days(365),
        )
        RemovalPolicies.of(self.memory).retain(
            apply_to_resource_types=["AWS::BedrockAgentCore::Memory"]
        )

        agent_runtime_artifact = agentcore.AgentRuntimeArtifact.from_asset(
            str(REPO_ROOT),
            file="strategist/Dockerfile",
            platform=ecr_assets.Platform.LINUX_ARM64,
        )

        self.runtime = agentcore.Runtime(
            self,
            "StrategistRuntime",
            runtime_name="juhta_strategist",
            description="Juhta Strategist: designs agents that fit the plan.",
            agent_runtime_artifact=agent_runtime_artifact,
            network_configuration=agentcore.RuntimeNetworkConfiguration.using_public_network(),
            protocol_configuration=agentcore.ProtocolType.HTTP,
            environment_variables={
                "AWS_REGION": self.region,
                "BEDROCK_MODEL_ID": bedrock_model_id,
                "GITHUB_REPOSITORY": github_repository,
                "AGENT_SECRET_ARN": agent_secret.secret_arn,
                "AGENTCORE_MEMORY_ID": self.memory.memory_id,
                # ADOT otherwise drops the Strands spans the dashboard reads.
                "OTEL_TRACES_SAMPLER": "always_on",
            },
            lifecycle_configuration=agentcore.LifecycleConfiguration(
                idle_runtime_session_timeout=Duration.minutes(5),
                max_lifetime=Duration.minutes(30),
            ),
            # Needs CloudWatch Transaction Search enabled for the account.
            tracing_enabled=True,
        )
        self.runtime.node.add_dependency(self.memory)
        self.memory.grant_read_short_term_memory(self.runtime)
        self.memory.grant_write(self.runtime)
        agent_secret.grant_read(self.runtime)

        model_access = iam.Policy(
            self,
            "StrategistAgentPermissions",
            roles=[self.runtime.role],
            statements=[
                iam.PolicyStatement(
                    sid="InvokeClaudeOnBedrock",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "bedrock:InvokeModel",
                        "bedrock:InvokeModelWithResponseStream",
                    ],
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
                f"AwsSolutions-IAM5[{finding}]": RUNTIME_DEFAULT_ROLE_REASON
                for finding in runtime_default_role_findings(self)
            },
        )
        acknowledge(model_access, CROSS_REGION_MODELS)
        acknowledge(agent_secret, NO_AUTOMATIC_ROTATION)

        CfnOutput(self, "StrategistRuntimeArn", value=self.runtime.agent_runtime_arn)
        CfnOutput(self, "StrategistRuntimeId", value=self.runtime.agent_runtime_id)
        CfnOutput(self, "StrategistMemoryId", value=self.memory.memory_id)
