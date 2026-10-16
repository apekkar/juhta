/**
 * Agent status, token usage and recent activity, from the Strands spans in `aws/spans`.
 * Spans are written when a step ends, so "working" means "a step just finished".
 */
import type { BedrockAgentCoreClient } from "@aws-sdk/client-bedrock-agentcore";
import {
  type CloudWatchLogsClient,
  GetQueryResultsCommand,
  StartQueryCommand,
} from "@aws-sdk/client-cloudwatch-logs";
import { type Config, invokeRuntime } from "./strategist";

export const AGENTS = [
  { key: "coordinator", name: "Coordinator", runtimeArn: (c: Config) => c.coordinatorRuntimeArn },
  { key: "strategist", name: "Strategist", runtimeArn: (c: Config) => c.runtimeArn },
  { key: "worker", name: "Worker", runtimeArn: (c: Config) => c.workerRuntimeArn },
] as const;
export type AgentKey = (typeof AGENTS)[number]["key"];

// USD per million tokens. Cache reads cost 0.1x input, cache writes 1.25x.
const PRICES: Record<string, { input: number; output: number }> = {
  "eu.anthropic.claude-haiku-4-5-20251001-v1:0": { input: 1, output: 5 },
};

export interface TokenUsage {
  runs: number;
  inputTokens: number;
  outputTokens: number;
  costUsd: number | null;
}

export interface Activity {
  at: Date;
  kind: "run" | "tool" | "request";
  label: string;
  failed: boolean;
}

export type Status = "checking" | "working" | "idle" | "failure";

const USAGE_QUERY = `filter name like /^invoke_agent/
| stats count(*) as runs,
  sum(\`attributes.gen_ai.usage.input_tokens\`) as inputTokens,
  sum(\`attributes.gen_ai.usage.output_tokens\`) as outputTokens,
  sum(\`attributes.gen_ai.usage.cache_read_input_tokens\`) as cacheReadTokens,
  sum(\`attributes.gen_ai.usage.cache_write_input_tokens\`) as cacheWriteTokens
  by \`resource.attributes.service.name\` as agent, \`attributes.gen_ai.request.model\` as model`;

const ACTIVITY_QUERY = `filter name like /^(invoke_agent|execute_tool)/
  or name = "POST /invocations" and \`status.code\` = "ERROR"
| fields @timestamp as at, name, \`resource.attributes.service.name\` as agent,
  \`status.code\` as status, \`attributes.gen_ai.usage.total_tokens\` as tokens
| sort @timestamp desc
| limit 200`;

const compact = new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 });

/** True when the agent's Runtime answers {"action": "ping"} (no model call). */
export async function pingAgent({
  client,
  runtimeArn,
}: {
  client: BedrockAgentCoreClient;
  runtimeArn: string;
}): Promise<boolean> {
  try {
    const { status } = await invokeRuntime({ client, runtimeArn, payload: { action: "ping" } });
    return status === "ok";
  } catch {
    return false;
  }
}

export async function loadUsage({
  client,
  start,
  end,
}: {
  client: CloudWatchLogsClient;
  start: Date;
  end: Date;
}): Promise<Record<AgentKey, TokenUsage>> {
  const usage = perAgent<TokenUsage>(() => ({
    runs: 0,
    inputTokens: 0,
    outputTokens: 0,
    costUsd: 0,
  }));
  for (const row of await runQuery({ client, query: USAGE_QUERY, start, end })) {
    const agent = usage[agentKey(row.agent)];
    if (!agent) continue;
    const [input, output, cacheRead, cacheWrite] = [
      row.inputTokens,
      row.outputTokens,
      row.cacheReadTokens,
      row.cacheWriteTokens,
    ].map((value) => Number(value ?? 0));
    const price = PRICES[row.model];
    agent.runs += Number(row.runs);
    agent.inputTokens += input;
    agent.outputTokens += output;
    const cost = !(input + output + cacheRead + cacheWrite)
      ? 0
      : price
        ? ((input + 0.1 * cacheRead + 1.25 * cacheWrite) * price.input + output * price.output) /
          1e6
        : null;
    agent.costUsd = agent.costUsd === null || cost === null ? null : agent.costUsd + cost;
  }
  return usage;
}

export async function loadActivity({
  client,
  start,
  end,
}: {
  client: CloudWatchLogsClient;
  start: Date;
  end: Date;
}): Promise<Record<AgentKey, Activity[]>> {
  const activity = perAgent<Activity[]>(() => []);
  for (const row of await runQuery({ client, query: ACTIVITY_QUERY, start, end })) {
    const failed = row.status === "ERROR";
    activity[agentKey(row.agent)]?.push({
      at: new Date(`${row.at.replace(" ", "T")}Z`),
      kind: activityKind(row.name),
      label: activityLabel({ name: row.name, tokens: row.tokens, failed }),
      failed,
    });
  }
  return activity;
}

export function agentStatus({
  ping,
  activity,
  now,
}: {
  ping: boolean | undefined;
  activity: Activity[];
  now: Date;
}): Status {
  if (ping === undefined) return "checking";
  const minutesAgo = (step: Activity) => (now.getTime() - step.at.getTime()) / 60_000;
  // A failed tool call isn't a failure: the model gets the error and tries again.
  const failedRecently = (step: Activity) =>
    step.failed && step.kind !== "tool" && minutesAgo(step) <= 60;
  if (!ping || activity.some(failedRecently)) return "failure";
  return activity.length && minutesAgo(activity[0]) <= 2 ? "working" : "idle";
}

function activityKind(name: string): Activity["kind"] {
  if (name.startsWith("execute_tool ")) return "tool";
  return name.startsWith("invoke_agent") ? "run" : "request";
}

function activityLabel({
  name,
  tokens,
  failed,
}: {
  name: string;
  tokens?: string;
  failed: boolean;
}): string {
  if (name.startsWith("execute_tool ")) return `Tool: ${name.slice("execute_tool ".length)}`;
  if (name.startsWith("invoke_agent")) {
    return failed ? "Run failed" : `Run · ${compact.format(Number(tokens ?? 0))} tokens`;
  }
  return "Request failed";
}

/** "juhta_coordinator.DEFAULT" → "coordinator" */
function agentKey(serviceName: string): AgentKey {
  return serviceName.replace(/^juhta_/, "").replace(/\.DEFAULT$/, "") as AgentKey;
}

function perAgent<T>(initial: () => T): Record<AgentKey, T> {
  return Object.fromEntries(AGENTS.map(({ key }) => [key, initial()])) as Record<AgentKey, T>;
}

async function runQuery({
  client,
  query,
  start,
  end,
}: {
  client: CloudWatchLogsClient;
  query: string;
  start: Date;
  end: Date;
}): Promise<Record<string, string>[]> {
  const { queryId } = await client.send(
    new StartQueryCommand({
      logGroupNames: ["aws/spans"],
      queryString: query,
      startTime: Math.floor(start.getTime() / 1000),
      endTime: Math.floor(end.getTime() / 1000),
    }),
  );
  for (;;) {
    const { status, results } = await client.send(new GetQueryResultsCommand({ queryId }));
    if (status === "Complete") {
      return results!.map((fields) =>
        Object.fromEntries(fields.map(({ field, value }) => [field!, value!])),
      );
    }
    if (status !== "Running" && status !== "Scheduled") throw new Error(`Query ${status}`);
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
}
