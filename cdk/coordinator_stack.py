"""CDK stack: Coordinator's AgentCore Memory, secret, Runtime, receiver Lambda and schedule.

The Runtime ARN is published in SSM for the Worker: a cross-stack reference both ways
would be a dependency cycle.
"""

from pathlib import Path

from aws_cdk import CfnOutput, Duration, Environment, RemovalPolicies, Stack, TimeZone
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_scheduler_targets as scheduler_targets
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_ssm as ssm
from constructs import Construct
from nag import (
    CROSS_REGION_MODELS,
    LAMBDA_BASIC_EXECUTION,
    NO_AUTOMATIC_ROTATION,
    RUNTIME_DEFAULT_ROLE_REASON,
    STRATEGIST_RUNTIME_EXPORT,
    WORKER_RUNTIME_EXPORT,
    acknowledge,
    runtime_default_role_findings,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


RECEIVER_FUNCTION_NAME = "juhta-coordinator-receiver"
COORDINATOR_RUNTIME_PARAMETER = "/juhta/runtimes/coordinator"


class CoordinatorStack(Stack):
    def __init__(  # noqa: PLR0913 - keyword-only config
        self,
        scope: Construct,
        construct_id: str,
        *,
        bedrock_model_id: str,
        strategist_runtime: agentcore.Runtime,
        slack_user_id: str,
        github_repository: str,
        worker_runtime: agentcore.Runtime,
        env: Environment,
    ) -> None:
        super().__init__(scope, construct_id, env=env)

        agent_secret = secretsmanager.Secret(
            self,
            "AgentSecret",
            secret_name="juhta/coordinator",  # noqa: S106 - a name, not a password
            description="Coordinator JSON: slack_bot_token, slack_signing_secret, github_pat",
        )

        memory = agentcore.Memory(
            self,
            "CoordinatorMemory",
            memory_name="JuhtaCoordinatorMemory",
            description="Coordinator conversations, one session per CLI session / Slack thread.",
            expiration_duration=Duration.days(365),
        )
        RemovalPolicies.of(memory).retain(apply_to_resource_types=["AWS::BedrockAgentCore::Memory"])

        self.runtime = agentcore.Runtime(
            self,
            "CoordinatorRuntime",
            runtime_name="juhta_coordinator",
            description="Juhta Coordinator: the agent you talk to; routes to the others.",
            agent_runtime_artifact=agentcore.AgentRuntimeArtifact.from_asset(
                str(REPO_ROOT),
                file="coordinator/Dockerfile",
                platform=ecr_assets.Platform.LINUX_ARM64,
            ),
            network_configuration=agentcore.RuntimeNetworkConfiguration.using_public_network(),
            protocol_configuration=agentcore.ProtocolType.HTTP,
            environment_variables={
                "AWS_REGION": self.region,
                "COORDINATOR_MODEL_ID": bedrock_model_id,
                "AGENTCORE_MEMORY_ID": memory.memory_id,
                "STRATEGIST_RUNTIME_ARN": strategist_runtime.agent_runtime_arn,
                "AGENT_SECRET_ARN": agent_secret.secret_arn,
                "SLACK_USER_ID": slack_user_id,
                "GITHUB_REPOSITORY": github_repository,
                "WORKER_RUNTIME_ARN": worker_runtime.agent_runtime_arn,
                # ADOT otherwise drops the Strands spans the dashboard reads.
                "OTEL_TRACES_SAMPLER": "always_on",
            },
            lifecycle_configuration=agentcore.LifecycleConfiguration(
                idle_runtime_session_timeout=Duration.minutes(5),
                max_lifetime=Duration.minutes(30),
            ),
            tracing_enabled=True,
        )
        self.runtime.node.add_dependency(memory)
        memory.grant_read_short_term_memory(self.runtime)
        memory.grant_write(self.runtime)
        agent_secret.grant_read(self.runtime)
        strategist_runtime.grant_invoke_runtime(self.runtime)
        worker_runtime.grant_invoke_runtime(self.runtime)
        ssm.StringParameter(
            self,
            "RuntimeArnParameter",
            parameter_name=COORDINATOR_RUNTIME_PARAMETER,
            string_value=self.runtime.agent_runtime_arn,
            description="Coordinator's Runtime ARN, for agents that report to it (the Worker).",
        )

        model_access = iam.Policy(
            self,
            "CoordinatorModelAccess",
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

        # Slack events and the daily schedule both reach Coordinator through the receiver.
        receiver = lambda_.Function(
            self,
            "CoordinatorReceiver",
            function_name=RECEIVER_FUNCTION_NAME,
            runtime=lambda_.Runtime.PYTHON_3_14,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset(
                str(REPO_ROOT / "coordinator_receiver"), exclude=["tests", "__pycache__"]
            ),
            timeout=Duration.minutes(5),  # async mode waits for Coordinator's reply
            reserved_concurrent_executions=100,
            environment={
                "AGENT_SECRET_ARN": agent_secret.secret_arn,
                "SLACK_USER_ID": slack_user_id,
                "COORDINATOR_RUNTIME_ARN": self.runtime.agent_runtime_arn,
            },
        )
        slack_events_url = receiver.add_function_url(auth_type=lambda_.FunctionUrlAuthType.NONE)
        agent_secret.grant_read(receiver)
        self.runtime.grant_invoke_runtime(receiver)
        # Self-invoke by name: referencing the function's own ARN would be a circular dependency.
        receiver.add_to_role_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[
                    f"arn:aws:lambda:{self.region}:{self.account}:function:{RECEIVER_FUNCTION_NAME}"
                ],
            )
        )

        # Via the receiver: Scheduler's universal target can't call InvokeAgentRuntime.
        # No retries, so a slow run can't produce a duplicate design.
        dead_letters = sqs.Queue(
            self,
            "CoordinatorDailyDeadLetters",
            retention_period=Duration.days(14),
            enforce_ssl=True,
        )
        scheduler.Schedule(
            self,
            "CoordinatorDaily",
            description="Daily: Strategist designs, Coordinator DMs a summary (run_daily).",
            schedule=scheduler.ScheduleExpression.cron(
                minute="0", hour="7", time_zone=TimeZone.EUROPE_HELSINKI
            ),
            target=scheduler_targets.LambdaInvoke(
                receiver,
                input=scheduler.ScheduleTargetInput.from_object(
                    {"coordinator_payload": {"action": "daily"}}
                ),
                retry_attempts=0,
                dead_letter_queue=dead_letters,
            ),
        )

        acknowledge(
            self.runtime,
            {
                **{
                    f"AwsSolutions-IAM5[{finding}]": RUNTIME_DEFAULT_ROLE_REASON
                    for finding in runtime_default_role_findings(self)
                },
                f"AwsSolutions-IAM5[Resource::{STRATEGIST_RUNTIME_EXPORT}/*]": (
                    "Invoke grant on Strategist's Runtime endpoints (<arn>/*)."
                ),
                f"AwsSolutions-IAM5[Resource::{WORKER_RUNTIME_EXPORT}/*]": (
                    "Invoke grant on the Worker's Runtime endpoints (<arn>/*), to queue issues."
                ),
            },
        )
        acknowledge(model_access, CROSS_REGION_MODELS)
        acknowledge(agent_secret, NO_AUTOMATIC_ROTATION)
        acknowledge(
            receiver,
            {
                LAMBDA_BASIC_EXECUTION: "CloudWatch Logs for this function only.",
                "AwsSolutions-IAM5[Resource::<CoordinatorRuntime57EFB771.AgentRuntimeArn>/*]": (
                    "Invoke grant on Coordinator's Runtime endpoints (<arn>/*)."
                ),
            },
        )
        acknowledge(
            dead_letters, {"AwsSolutions-SQS3": "This queue is the schedule's dead-letter queue."}
        )
        acknowledge(
            self,
            {
                "AwsSolutions-IAM5[Resource::<CoordinatorReceiver25C8FC6E.Arn>:*]": (
                    "Scheduler may invoke the receiver's versions/aliases (<arn>:*)."
                ),
            },
        )

        CfnOutput(self, "CoordinatorRuntimeArn", value=self.runtime.agent_runtime_arn)
        CfnOutput(self, "SlackEventsUrl", value=slack_events_url.url)
