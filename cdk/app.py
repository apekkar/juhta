#!/usr/bin/env python3
"""CDK app entrypoint. Reads config from the repo-root .env or the CI environment."""

import os
from pathlib import Path

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks
from coordinator_stack import CoordinatorStack
from dashboard_stack import DashboardStack
from dotenv import load_dotenv
from github_stack import GithubStack
from strategist_stack import StrategistStack
from worker_stack import WorkerStack

REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env")

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ["AWS_REGION"],
)

app = cdk.App()

strategist = StrategistStack(
    app,
    "JuhtaStrategistStack",
    bedrock_model_id=os.environ["BEDROCK_MODEL_ID"],
    github_repository=os.environ["GITHUB_REPOSITORY"],
    env=env,
)
worker = WorkerStack(
    app,
    "JuhtaWorkerStack",
    bedrock_model_id=os.environ["BEDROCK_MODEL_ID"],
    github_repository=os.environ["GITHUB_REPOSITORY"],
    env=env,
)

coordinator = CoordinatorStack(
    app,
    "JuhtaCoordinatorStack",
    bedrock_model_id=os.environ["COORDINATOR_MODEL_ID"],
    strategist_runtime=strategist.runtime,
    slack_user_id=os.environ["SLACK_USER_ID"],
    github_repository=os.environ["GITHUB_REPOSITORY"],
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
    dist_path=str(REPO_ROOT / "dashboard" / "dist"),  # built by `npm run build`
    env=env,
)
GithubStack(
    app,
    "JuhtaGithubStack",
    github_repository=os.environ["GITHUB_REPOSITORY"],
    github_repository_owner_id=os.environ["GITHUB_REPOSITORY_OWNER_ID"],
    github_repository_id=os.environ["GITHUB_REPOSITORY_ID"],
    bedrock_tests_model_id=os.environ["BEDROCK_MODEL_ID"],
    env=env,
)

cdk.Validations.of(app).add_plugins(AwsSolutionsChecks(app))

app.synth()
