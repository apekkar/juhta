"""Worker test config: env from the repo's .env plus test defaults."""

import json
import os
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(REPO_ROOT / ".env")
for name, value in {
    "AWS_REGION": "eu-west-1",
    "GITHUB_REPOSITORY": "octocat/juhta",
    "AGENT_SECRET_ARN": "juhta/worker",
    "COORDINATOR_RUNTIME_ARN": "arn:aws:bedrock-agentcore:eu-west-1:123:runtime/test-coordinator",
}.items():
    os.environ.setdefault(name, value)


@pytest.fixture(scope="session")
def github_token() -> str:
    """The PAT from Secrets Manager if readable, else a dummy."""
    try:
        secret = boto3.client("secretsmanager", region_name=os.environ["AWS_REGION"])
        value = secret.get_secret_value(SecretId=os.environ["AGENT_SECRET_ARN"])
    except BotoCoreError, ClientError:
        return "test-token"
    token: str = json.loads(value["SecretString"])["github_pat"]
    return token
