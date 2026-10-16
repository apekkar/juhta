"""cdk-nag must pass with and without a known AWS account."""

from pathlib import Path

import aws_cdk as cdk
import pytest
from cdk_nag import AwsSolutionsChecks
from coordinator_stack import CoordinatorStack
from dashboard_stack import DashboardStack
from github_stack import GithubStack
from strategist_stack import StrategistStack
from worker_stack import WorkerStack


@pytest.mark.parametrize("account", [None, "123456789012"])
def test_all_stacks_pass_cdk_nag(tmp_path: Path, account: str | None) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>")
    env = cdk.Environment(account=account, region="eu-west-1")
    app = cdk.App(outdir=str(tmp_path / "cdk.out"))
    strategist = StrategistStack(
        app,
        "JuhtaStrategistStack",
        bedrock_model_id="eu.anthropic.claude-sonnet-4-6",
        github_repository="octocat/juhta",
        env=env,
    )
    worker = WorkerStack(
        app,
        "JuhtaWorkerStack",
        bedrock_model_id="eu.anthropic.claude-sonnet-4-6",
        github_repository="octocat/juhta",
        env=env,
    )

    coordinator = CoordinatorStack(
        app,
        "JuhtaCoordinatorStack",
        bedrock_model_id="eu.anthropic.claude-haiku-4-5-20251001-v1:0",
        strategist_runtime=strategist.runtime,
        slack_user_id="U123",
        github_repository="octocat/juhta",
        worker_runtime=worker.runtime,
        env=env,
    )
    DashboardStack(
        app,
        "JuhtaDashboardStack",
        memory=strategist.memory,
        runtime=strategist.runtime,
        coordinator_runtime=coordinator.runtime,
        worker_runtime=worker.runtime,
        dist_path=str(dist),
        env=env,
    )
    GithubStack(
        app,
        "JuhtaGithubStack",
        github_repository="octocat/juhta",
        github_repository_owner_id="1111",
        github_repository_id="2222",
        bedrock_tests_model_id="eu.anthropic.claude-sonnet-4-6",
        env=env,
    )
    cdk.Validations.of(app).add_plugins(AwsSolutionsChecks(app))

    app.synth()  # raises ValidationFailed on any unacknowledged finding
