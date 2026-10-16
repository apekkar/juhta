"""cdk-nag helpers: acknowledgements and findings shared across stacks."""

from aws_cdk import Acknowledgment, Stack, Token, Validations
from constructs import IConstruct


def acknowledge(construct: IConstruct, rules: dict[str, str]) -> None:
    """Accept nag findings on `construct` (and its children): {rule id: reason}."""
    Validations.of(construct).acknowledge(
        *(Acknowledgment(id=rule, reason=reason) for rule, reason in rules.items())
    )


def account_in_findings(stack: Stack) -> str:
    """Account in finding IDs: a placeholder when synthesized without credentials."""
    return "<AWS::AccountId>" if Token.is_unresolved(stack.account) else stack.account


def runtime_default_role_findings(stack: Stack) -> list[str]:
    """Findings on the AgentCore Runtime construct's default execution role."""
    region, account = stack.region, account_in_findings(stack)
    return [
        "Resource::*",
        f"Resource::arn:<AWS::Partition>:bedrock-agentcore:{region}:{account}"
        ":workload-identity-directory/default/workload-identity/*",
        f"Resource::arn:<AWS::Partition>:logs:{region}:{account}:log-group:*",
        f"Resource::arn:<AWS::Partition>:logs:{region}:{account}"
        ":log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*",
        f"Resource::arn:<AWS::Partition>:logs:{region}:{account}"
        ":log-group:/aws/bedrock-agentcore/runtimes/*",
    ]


RUNTIME_DEFAULT_ROLE_REASON = (
    "AgentCore Runtime construct's default execution role (CloudWatch Logs, X-Ray, metrics, "
    "workload identity): CDK-managed, scoped by service."
)
CROSS_REGION_MODELS = {
    "AwsSolutions-IAM5[Resource::arn:aws:bedrock:*::foundation-model/anthropic.*]": (
        "Cross-region inference profiles route to the model in any EU region at call time; "
        "scoped to Anthropic models."
    )
}
LAMBDA_BASIC_EXECUTION = (
    "AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/"
    "AWSLambdaBasicExecutionRole]"
)
# Logical IDs of Runtime ARN exports, as seen by stacks that import them.
STRATEGIST_RUNTIME_EXPORT = (
    "JuhtaStrategistStack:ExportsOutputFnGetAttStrategistRuntime268AF648AgentRuntimeArn1CEE4092"
)
COORDINATOR_RUNTIME_EXPORT = (
    "JuhtaCoordinatorStack:ExportsOutputFnGetAttCoordinatorRuntime57EFB771AgentRuntimeArn3C5B4556"
)
WORKER_RUNTIME_EXPORT = (
    "JuhtaWorkerStack:ExportsOutputFnGetAttWorkerRuntime9D47D1E7AgentRuntimeArn2E346FF7"
)
NO_AUTOMATIC_ROTATION = {
    "AwsSolutions-SMG4": (
        "Holds third-party tokens (Slack, GitHub PATs) that AWS can't rotate; "
        "PATs expire in 90 days and are replaced by hand."
    )
}
