import {
  type BedrockAgentCoreClient,
  type Event,
  InvokeAgentRuntimeCommand,
  ListEventsCommand,
} from "@aws-sdk/client-bedrock-agentcore";
import type { CloudWatchLogsClient } from "@aws-sdk/client-cloudwatch-logs";

// Must match ACTOR_ID / SOURCES in strategist/agent.py (one Memory session per source).
const ACTOR_ID = "strategist";
export const SOURCES = ["daily", "on-demand"] as const;
export type Source = (typeof SOURCES)[number];

export interface Config {
  region: string;
  userPoolId: string;
  userPoolClientId: string;
  identityPoolId: string;
  memoryId: string;
  runtimeArn: string;
  runtimeName: string;
  coordinatorRuntimeArn: string;
  workerRuntimeArn: string;
}

export interface Clients {
  agentCore: BedrockAgentCoreClient;
  logs: CloudWatchLogsClient;
}

/** Mirrors AgentDesign in strategist/agent.py. */
export interface AgentDesign {
  name: string;
  role: string;
  description: string;
  system_prompt: string;
  capabilities: string[];
  implementation_effort: "low" | "medium" | "high";
  plan_step: string;
}

export interface DesignRecord {
  request: string;
  design: AgentDesign;
  createdAt: Date;
  source: Source;
}

export async function loadConfig(): Promise<Config> {
  return (await fetch("/config.json")).json();
}

function toDesignRecord({ event, source }: { event: Event; source: Source }): DesignRecord {
  const [request, response] = event.payload!.map((item) => item.conversational!.content!.text!);
  return { request, design: JSON.parse(response), createdAt: event.eventTimestamp!, source };
}

export async function listDesigns({
  client,
  config,
}: {
  client: BedrockAgentCoreClient;
  config: Config;
}): Promise<DesignRecord[]> {
  const perSource = await Promise.all(
    SOURCES.map(async (source) => {
      const { events } = await client.send(
        new ListEventsCommand({
          memoryId: config.memoryId,
          actorId: ACTOR_ID,
          sessionId: source,
          includePayloads: true,
          maxResults: 100,
        }),
      );
      return events!.map((event) => toDesignRecord({ event, source }));
    }),
  );
  return perSource.flat().sort((a, b) => b.createdAt.getTime() - a.createdAt.getTime());
}

export async function invokeRuntime({
  client,
  runtimeArn,
  payload,
}: {
  client: BedrockAgentCoreClient;
  runtimeArn: string;
  payload: Record<string, string>;
}) {
  const { response } = await client.send(
    new InvokeAgentRuntimeCommand({
      agentRuntimeArn: runtimeArn,
      runtimeSessionId: crypto.randomUUID(),
      contentType: "application/json",
      payload: new TextEncoder().encode(JSON.stringify(payload)),
    }),
  );
  return JSON.parse(await response!.transformToString());
}

export async function designNow({
  client,
  config,
  prompt,
}: {
  client: BedrockAgentCoreClient;
  config: Config;
  prompt: string;
}): Promise<AgentDesign> {
  return (await invokeRuntime({ client, runtimeArn: config.runtimeArn, payload: { prompt } }))
    .design;
}

/** The current plan, read by the Runtime without a model call. */
export async function readPlan({
  client,
  config,
}: {
  client: BedrockAgentCoreClient;
  config: Config;
}): Promise<string> {
  return (
    await invokeRuntime({ client, runtimeArn: config.runtimeArn, payload: { action: "plan" } })
  ).plan;
}

export async function fileDesignAsIssue({
  client,
  config,
  record,
}: {
  client: BedrockAgentCoreClient;
  config: Config;
  record: DesignRecord;
}): Promise<string> {
  const prompt =
    `File this Strategist design as a GitHub issue (not queued). Design "${record.design.name}" ` +
    `(${record.source}, ${record.createdAt.toISOString()}):\n${JSON.stringify(record.design)}`;
  const { reply } = await invokeRuntime({
    client,
    runtimeArn: config.coordinatorRuntimeArn,
    payload: { prompt, session_id: `dashboard-${crypto.randomUUID()}` },
  });
  return reply;
}
