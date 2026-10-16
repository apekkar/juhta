import type { BedrockAgentCoreClient } from "@aws-sdk/client-bedrock-agentcore";
import {
  type CloudWatchLogsClient,
  GetQueryResultsCommand,
  StartQueryCommand,
} from "@aws-sdk/client-cloudwatch-logs";
import { afterEach, describe, expect, it, vi } from "vitest";
import { type Activity, agentStatus, loadActivity, loadUsage, pingAgent } from "./agents";

const HAIKU = "eu.anthropic.claude-haiku-4-5-20251001-v1:0";
const START = new Date("2026-10-01T00:00:00Z");
const NOW = new Date("2026-10-06T12:00:00Z");

const row = (fields: Record<string, string>) =>
  Object.entries(fields).map(([field, value]) => ({ field, value }));

function logsReturning(...rows: Record<string, string>[]) {
  const send = vi.fn(async (command: unknown) =>
    command instanceof StartQueryCommand
      ? { queryId: "q-1" }
      : { status: "Complete", results: rows.map(row) },
  );
  return { client: { send } as unknown as CloudWatchLogsClient, send };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("loadUsage", () => {
  it("sums tokens per agent and prices known models", async () => {
    const { client, send } = logsReturning(
      {
        agent: "juhta_coordinator.DEFAULT",
        model: HAIKU,
        runs: "2",
        inputTokens: "1000000",
        outputTokens: "100000",
        cacheReadTokens: "0",
        cacheWriteTokens: "0",
      },
      {
        agent: "juhta_strategist.DEFAULT",
        model: "eu.anthropic.claude-sonnet-4-6",
        runs: "4",
        inputTokens: "10",
      },
    );

    const usage = await loadUsage({ client, start: START, end: NOW });

    expect(usage.coordinator).toEqual({
      runs: 2,
      inputTokens: 1_000_000,
      outputTokens: 100_000,
      costUsd: 1.5,
    });
    expect(usage.strategist).toEqual({ runs: 4, inputTokens: 10, outputTokens: 0, costUsd: null });
    const start = send.mock.calls[0][0] as StartQueryCommand;
    expect(start.input).toMatchObject({
      logGroupNames: ["aws/spans"],
      startTime: START.getTime() / 1000,
      endTime: NOW.getTime() / 1000,
    });
  });

  it("keeps the cost when a run on an unpriced model used no tokens", async () => {
    const { client } = logsReturning(
      { agent: "juhta_strategist.DEFAULT", model: HAIKU, runs: "1", inputTokens: "1000000" },
      { agent: "juhta_strategist.DEFAULT", model: "eu.anthropic.claude-sonnet-4-6", runs: "3" },
    );

    const usage = await loadUsage({ client, start: START, end: NOW });

    expect(usage.strategist.costUsd).toBe(1);
  });

  it("counts the Worker's runs too", async () => {
    const { client } = logsReturning({
      agent: "juhta_worker.DEFAULT",
      model: HAIKU,
      runs: "1",
      inputTokens: "1000000",
    });

    const usage = await loadUsage({ client, start: START, end: NOW });

    expect(usage.worker).toEqual({ runs: 1, inputTokens: 1_000_000, outputTokens: 0, costUsd: 1 });
  });

  it("ignores agents outside Juhta's current ones", async () => {
    const { client } = logsReturning({ agent: "juhta_ideator.DEFAULT", model: HAIKU, runs: "1" });

    const usage = await loadUsage({ client, start: START, end: NOW });

    expect(usage.coordinator.runs).toBe(0);
    expect(usage.strategist.runs).toBe(0);
  });

  it("waits until the query completes", async () => {
    vi.useFakeTimers();
    const send = vi
      .fn()
      .mockResolvedValueOnce({ queryId: "q-1" })
      .mockResolvedValueOnce({ status: "Running" })
      .mockResolvedValueOnce({ status: "Complete", results: [] });

    const pending = loadUsage({
      client: { send } as unknown as CloudWatchLogsClient,
      start: START,
      end: NOW,
    });
    await vi.runAllTimersAsync();

    expect((await pending).coordinator.runs).toBe(0);
    expect(send.mock.calls[2][0]).toBeInstanceOf(GetQueryResultsCommand);
  });
});

describe("loadActivity", () => {
  it("lists each agent's steps newest first, labelled", async () => {
    const { client } = logsReturning(
      {
        at: "2026-10-06 11:59:00.000",
        name: "invoke_agent Strands Agents",
        agent: "juhta_coordinator.DEFAULT",
        status: "OK",
        tokens: "4070",
      },
      {
        at: "2026-10-06 11:58:00.000",
        name: "execute_tool ask_strategist",
        agent: "juhta_coordinator.DEFAULT",
        status: "OK",
      },
      {
        at: "2026-10-06 10:00:00.000",
        name: "POST /invocations",
        agent: "juhta_strategist.DEFAULT",
        status: "ERROR",
      },
    );

    const activity = await loadActivity({ client, start: START, end: NOW });

    expect(activity.coordinator).toEqual([
      {
        at: new Date("2026-10-06T11:59:00Z"),
        kind: "run",
        label: "Run · 4.1K tokens",
        failed: false,
      },
      {
        at: new Date("2026-10-06T11:58:00Z"),
        kind: "tool",
        label: "Tool: ask_strategist",
        failed: false,
      },
    ]);
    expect(activity.strategist).toEqual([
      {
        at: new Date("2026-10-06T10:00:00Z"),
        kind: "request",
        label: "Request failed",
        failed: true,
      },
    ]);
  });
});

describe("agentStatus", () => {
  const step = (
    minutesAgo: number,
    { failed = false, kind = "run" }: { failed?: boolean; kind?: Activity["kind"] } = {},
  ): Activity => ({
    at: new Date(NOW.getTime() - minutesAgo * 60_000),
    kind,
    label: "Run",
    failed,
  });

  it("is checking until the ping answers", () => {
    expect(agentStatus({ ping: undefined, activity: [], now: NOW })).toBe("checking");
  });

  it("is a failure when the ping fails", () => {
    expect(agentStatus({ ping: false, activity: [], now: NOW })).toBe("failure");
  });

  it("is a failure after a failed step in the last hour", () => {
    const failedRun = step(30, { failed: true });
    expect(agentStatus({ ping: true, activity: [failedRun], now: NOW })).toBe("failure");
    const failedRequest = step(30, { failed: true, kind: "request" });
    expect(agentStatus({ ping: true, activity: [failedRequest], now: NOW })).toBe("failure");
    expect(agentStatus({ ping: true, activity: [step(90, { failed: true })], now: NOW })).toBe(
      "idle",
    );
  });

  it("isn't a failure when a tool call errs: the model reads the error and retries", () => {
    const failedTool = step(1, { failed: true, kind: "tool" });

    expect(agentStatus({ ping: true, activity: [failedTool], now: NOW })).toBe("working");
  });

  it("is working right after a step", () => {
    expect(agentStatus({ ping: true, activity: [step(1)], now: NOW })).toBe("working");
    expect(agentStatus({ ping: true, activity: [step(10)], now: NOW })).toBe("idle");
  });
});

describe("pingAgent", () => {
  const runtimeArn = "arn:aws:bedrock-agentcore:eu-west-1:123:runtime/juhta_coordinator";

  it("is true when the agent answers ok", async () => {
    const send = vi.fn().mockResolvedValue({
      response: { transformToString: async () => JSON.stringify({ status: "ok" }) },
    });

    expect(
      await pingAgent({ client: { send } as unknown as BedrockAgentCoreClient, runtimeArn }),
    ).toBe(true);
    expect(JSON.parse(new TextDecoder().decode(send.mock.calls[0][0].input.payload))).toEqual({
      action: "ping",
    });
  });

  it("is false when the call fails", async () => {
    const send = vi.fn().mockRejectedValue(new Error("RuntimeClientError"));

    expect(
      await pingAgent({ client: { send } as unknown as BedrockAgentCoreClient, runtimeArn }),
    ).toBe(false);
  });
});
