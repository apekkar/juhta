"""Strategist: a Strands agent on Amazon Bedrock that designs automation agents.

AgentCore Memory holds one session per source ("daily", "on-demand"), one event per design.
Each design starts from an empty conversation; the current plan and all past designs go in
the system prompt instead.
"""

import json
import os
from typing import Any, Literal, Self, cast

import boto3
from bedrock_agentcore.memory import MemoryClient
from mcp import StdioServerParameters, stdio_client
from pydantic import BaseModel, Field
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient

ACTOR_ID = "strategist"
# Memory session per source; also the dashboard's filter values.
Source = Literal["daily", "on-demand"]
SOURCES: tuple[Source, ...] = ("daily", "on-demand")
PLAN_PATH = "docs/agent-pipeline-plan.md"

DAILY_PROMPT = (
    "Propose the next most useful agent: pick the earliest step in the plan's build order that "
    "has no design yet, check the repository for what already exists, and design it."
)

SYSTEM_PROMPT = """You are the Strategist Agent. You design new agents for automation workflows.

Every earlier design is listed below. Use them to:
- Never repeat an earlier design; pick something not yet designed
- Build on or refine earlier ideas when the new request is related
- Point out explicitly when a request overlaps with something you already designed

You have read-only access to the GitHub repository {github_repository}, which holds the agents
and infrastructure built so far. The current plan is included below. When designing:
- Fit the plan: say which planned piece or build-order step the design belongs to, or why it
  is worth adding outside the plan.
- Check the repository (README, agent code, open issues) for what already exists, and build on
  it instead of proposing it again.
- Follow the plan's constraints (e.g. AgentCore Runtime per agent, GitHub Actions as the event
  glue, least-privilege access)."""


class AgentDesign(BaseModel):
    name: str
    role: str
    description: str
    system_prompt: str
    capabilities: list[str]
    implementation_effort: Literal["low", "medium", "high"]
    plan_step: str = Field(
        description="The plan step this design covers, e.g. 'Build order 3: Worker agent', "
        "or 'Outside the plan: <why it is worth adding>'."
    )


class StrategistAgent(Agent):
    def __init__(
        self,
        *,
        memory_id: str,
        model_id: str,
        region: str,
        github_repository: str,
        github_token: str,
    ) -> None:
        """model_id must be a cross-region inference profile ID."""
        base_system_prompt = SYSTEM_PROMPT.format(github_repository=github_repository)
        github = MCPClient(
            lambda: stdio_client(
                StdioServerParameters(
                    command="github-mcp-server",
                    args=["stdio", "--read-only", "--toolsets=repos,issues"],
                    env={"GITHUB_PERSONAL_ACCESS_TOKEN": github_token},
                )
            )
        )
        super().__init__(
            model=BedrockModel(model_id=model_id, region_name=region),
            system_prompt=base_system_prompt,
            tools=[github],
            structured_output_model=AgentDesign,
        )
        self._base_system_prompt = base_system_prompt
        self._github_owner, self._github_repo = github_repository.split("/")
        self._memory_id = memory_id
        self._memory_client = MemoryClient(region_name=region)

    @classmethod
    def from_env(cls) -> Self:
        region = os.environ["AWS_REGION"]
        secret = boto3.client("secretsmanager", region_name=region).get_secret_value(
            SecretId=os.environ["AGENT_SECRET_ARN"]
        )
        return cls(
            memory_id=os.environ["AGENTCORE_MEMORY_ID"],
            model_id=os.environ["BEDROCK_MODEL_ID"],
            region=region,
            github_repository=os.environ["GITHUB_REPOSITORY"],
            github_token=json.loads(secret["SecretString"])["github_pat"],
        )

    def design(self, *, workflow_description: str, source: Source) -> AgentDesign:
        earlier = "\n".join(
            f"- {d.name} [{d.plan_step}]: {d.description}" for d in self.past_designs()
        )
        self.system_prompt = (
            f"{self._base_system_prompt}\n\nCurrent plan:\n\n{self.read_plan()}"
            f"\n\nEarlier designs:\n\n{earlier or '(none yet)'}"
        )
        self.messages = []
        design = cast(AgentDesign, self(workflow_description).structured_output)
        self._memory_client.create_event(
            memory_id=self._memory_id,
            actor_id=ACTOR_ID,
            session_id=source,
            messages=[
                (workflow_description, "USER"),
                (design.model_dump_json(), "ASSISTANT"),
            ],
        )
        return design

    def past_designs(self) -> list[AgentDesign]:
        return [AgentDesign.model_validate(record["design"]) for record in self._design_records()]

    def recent_designs(self, *, limit: int) -> list[dict[str, Any]]:
        records = sorted(self._design_records(), key=lambda r: r["created_at"], reverse=True)
        return records[:limit]

    def _design_records(self) -> list[dict[str, Any]]:
        return [
            {
                "source": source,
                "created_at": event["eventTimestamp"].isoformat(),
                "request": event["payload"][0]["conversational"]["content"]["text"],
                "design": json.loads(event["payload"][-1]["conversational"]["content"]["text"]),
            }
            for source in SOURCES
            for event in self._memory_client.list_events(
                memory_id=self._memory_id, actor_id=ACTOR_ID, session_id=source
            )
        ]

    def read_plan(self) -> str:
        result = self.tool.get_file_contents(
            owner=self._github_owner,
            repo=self._github_repo,
            path=PLAN_PATH,
            record_direct_tool_call=False,
        )
        # GitHub failures come back as an error result, not an exception.
        if result["status"] == "error":
            raise RuntimeError(f"Reading {PLAN_PATH} from GitHub failed: {result['content']}")
        # The last content item is the file itself (earlier ones are status text).
        return str(result["content"][-1]["text"])
