"""CDK stack: Juhta dashboard — static React app on S3 + CloudFront, Cognito login.

No backend API: a Cognito identity pool gives the browser AWS credentials to call AgentCore
and CloudWatch Logs directly. Runtime config is written as config.json at deploy time.
"""

from aws_cdk import CfnOutput, Duration, Environment, RemovalPolicy, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_cognito_identitypool as identitypool
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3_deployment
from constructs import Construct
from nag import (
    COORDINATOR_RUNTIME_EXPORT,
    LAMBDA_BASIC_EXECUTION,
    STRATEGIST_RUNTIME_EXPORT,
    WORKER_RUNTIME_EXPORT,
    account_in_findings,
    acknowledge,
)


class DashboardStack(Stack):
    def __init__(  # noqa: PLR0913 - keyword-only config
        self,
        scope: Construct,
        construct_id: str,
        *,
        memory: agentcore.Memory,
        runtime: agentcore.Runtime,
        coordinator_runtime: agentcore.Runtime,
        worker_runtime: agentcore.Runtime,
        dist_path: str,
        env: Environment,
    ) -> None:
        super().__init__(scope, construct_id, env=env)

        bucket = s3.Bucket(
            self,
            "SiteBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        distribution = cloudfront.Distribution(
            self,
            "Distribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                # Otherwise browsers may show an old index.html after a deploy.
                response_headers_policy=cloudfront.ResponseHeadersPolicy(
                    self,
                    "RevalidateOnLoad",
                    custom_headers_behavior=cloudfront.ResponseCustomHeadersBehavior(
                        custom_headers=[
                            cloudfront.ResponseCustomHeader(
                                header="Cache-Control", value="no-cache", override=True
                            )
                        ]
                    ),
                ),
            ),
            default_root_object="index.html",
        )

        user_pool = cognito.UserPool(
            self,
            "UserPool",
            self_sign_up_enabled=False,
            password_policy=cognito.PasswordPolicy(
                min_length=14,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
            ),
            sign_in_aliases=cognito.SignInAliases(email=True),
            removal_policy=RemovalPolicy.DESTROY,
        )
        user_pool_client = user_pool.add_client(
            "WebClient",
            auth_flows=cognito.AuthFlow(user_srp=True),
            refresh_token_validity=Duration.days(1),
        )
        identity_pool = identitypool.IdentityPool(
            self,
            "IdentityPool",
            authentication_providers=identitypool.IdentityPoolAuthenticationProviders(
                user_pools=[
                    identitypool.UserPoolAuthenticationProvider(
                        user_pool=user_pool, user_pool_client=user_pool_client
                    )
                ]
            ),
        )
        memory.grant_read_short_term_memory(identity_pool.authenticated_role)
        runtime.grant_invoke_runtime(identity_pool.authenticated_role)
        coordinator_runtime.grant_invoke_runtime(identity_pool.authenticated_role)
        worker_runtime.grant_invoke_runtime(identity_pool.authenticated_role)
        # Agent cards: token usage and activity from the agents' traces.
        spans_log_group = f"arn:aws:logs:{self.region}:{self.account}:log-group:aws/spans:*"
        identity_pool.authenticated_role.add_to_principal_policy(
            iam.PolicyStatement(actions=["logs:StartQuery"], resources=[spans_log_group])
        )
        identity_pool.authenticated_role.add_to_principal_policy(
            iam.PolicyStatement(actions=["logs:GetQueryResults"], resources=["*"])
        )

        s3_deployment.BucketDeployment(
            self,
            "Site",
            destination_bucket=bucket,
            distribution=distribution,
            sources=[
                s3_deployment.Source.asset(dist_path),
                s3_deployment.Source.json_data(
                    "config.json",
                    {
                        "region": self.region,
                        "userPoolId": user_pool.user_pool_id,
                        "userPoolClientId": user_pool_client.user_pool_client_id,
                        "identityPoolId": identity_pool.identity_pool_id,
                        "memoryId": memory.memory_id,
                        "runtimeArn": runtime.agent_runtime_arn,
                        "runtimeName": runtime.agent_runtime_name,
                        "coordinatorRuntimeArn": coordinator_runtime.agent_runtime_arn,
                        "workerRuntimeArn": worker_runtime.agent_runtime_arn,
                    },
                ),
            ],
        )

        acknowledge(
            distribution,
            {
                "AwsSolutions-CFR1": "Personal dashboard; no geo restriction needed.",
                "AwsSolutions-CFR2": "No WAF: static files; all data access needs Cognito login.",
                "AwsSolutions-CFR3": "No access logs for a personal static site.",
                "AwsSolutions-CFR4": "Default CloudFront domain; TLS policy needs a custom domain.",
            },
        )
        acknowledge(bucket, {"AwsSolutions-S1": "Private bucket, read only by CloudFront (OAC)."})
        acknowledge(
            user_pool,
            {
                "AwsSolutions-COG2": "One admin-created user; the login form has no MFA step yet.",
                "AwsSolutions-COG8": "Threat protection is a paid tier; one admin-created user.",
            },
        )
        acknowledge(
            identity_pool,
            {
                "AwsSolutions-IAM5[Resource::*]": (
                    "logs:GetQueryResults can't be scoped to resources (reads a query by ID)."
                ),
                "AwsSolutions-IAM5[Resource::arn:aws:logs:"
                f"{self.region}:{account_in_findings(self)}:log-group:aws/spans:*]": (
                    "StartQuery on the aws/spans log group only (<arn>:* is its log streams)."
                ),
                f"AwsSolutions-IAM5[Resource::{STRATEGIST_RUNTIME_EXPORT}/*]": (
                    "Invoke grant on Strategist's Runtime endpoints (<arn>/*)."
                ),
                f"AwsSolutions-IAM5[Resource::{WORKER_RUNTIME_EXPORT}/*]": (
                    "Invoke grant on the Worker's Runtime endpoints (<arn>/*), to ping it."
                ),
                f"AwsSolutions-IAM5[Resource::{COORDINATOR_RUNTIME_EXPORT}/*]": (
                    "Invoke grant on Coordinator's Runtime endpoints (<arn>/*)."
                ),
            },
        )
        bucket_deployment = "CDK BucketDeployment's Lambda copies the build into the site bucket."
        acknowledge(
            self,
            {
                LAMBDA_BASIC_EXECUTION: bucket_deployment,
                "AwsSolutions-L1": "CDK BucketDeployment's Lambda runtime is chosen by CDK.",
                **{
                    f"AwsSolutions-IAM5[{finding}]": bucket_deployment
                    for finding in (
                        "Action::s3:Abort*",
                        "Action::s3:DeleteObject*",
                        "Action::s3:GetBucket*",
                        "Action::s3:GetObject*",
                        "Action::s3:List*",
                        "Resource::*",
                        "Resource::<SiteBucket397A1860.Arn>/*",
                        "Resource::arn:<AWS::Partition>:s3:::cdk-hnb659fds-assets-"
                        f"{account_in_findings(self)}-{self.region}/*",
                    )
                },
            },
        )

        CfnOutput(self, "DashboardUrl", value=f"https://{distribution.distribution_domain_name}")
        CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)
