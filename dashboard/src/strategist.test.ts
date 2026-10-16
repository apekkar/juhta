import {
  type BedrockAgentCoreClient,
  InvokeAgentRuntimeCommand,
  type ListEventsCommand,
} from "@aws-sdk/client-bedrock-agentcore";
import { describe, expect, it, vi } from "vitest";
import {
  type AgentDesign,
  type Config,
  designNow,
  fileDesignAsIssue,
  listDesigns,
  readPlan,
} from "./strategist";

const CONFIG: Config = {
  region: "eu-west-1",
  userPoolId: "pool",
  userPoolClientId: "client",
  identityPoolId: "identity",
  memoryId: "mem-1",
  runtimeArn: "arn:aws:bedrock-agentcore:eu-west-1:123:runtime/juhta_strategist",
  runtimeName: "juhta_strategist",
  coordinatorRuntimeArn: "arn:aws:bedrock-agentcore:eu-west-1:123:runtime/juhta_coordinator",
  workerRuntimeArn: "arn:aws:bedrock-agentcore:eu-west-1:123:runtime/juhta_worker",
};

const DESIGN: AgentDesign = {
  name: "InboxTriage",
  role: "classifier",
  description: "Sorts incoming email",
  system_prompt: "You triage email.",
  capabilities: ["classify", "route"],
  implementation_effort: "low",
  plan_step: "Build order 3: Worker agent",
};

function designEvent({ request, at }: { request: string; at: string }) {
  return {
    eventTimestamp: new Date(at),
    payload: [
      { conversational: { role: "USER", content: { text: request } } },
      { conversational: { role: "ASSISTANT", content: { text: JSON.stringify(DESIGN) } } },
    ],
  };
}

function clientReturning(output: unknown) {
  const send = vi.fn().mockResolvedValue(output);
  return { client: { send } as unknown as BedrockAgentCoreClient, send };
}

describe("listDesigns", () => {
  it("merges both source sessions newest first, tagged with their source", async () => {
    const send = vi.fn().mockImplementation(async (command: ListEventsCommand) => ({
      events:
        command.input.sessionId === "daily"
          ? [designEvent({ request: "daily run", at: "2026-10-02T04:00:00Z" })]
          : [designEvent({ request: "manual", at: "2026-10-01T08:00:00Z" })],
    }));
    const client = { send } as unknown as BedrockAgentCoreClient;

    const records = await listDesigns({ client, config: CONFIG });

    expect(records.map((r) => [r.request, r.source])).toEqual([
      ["daily run", "daily"],
      ["manual", "on-demand"],
    ]);
    expect(records[0].design).toEqual(DESIGN);
    expect(send.mock.calls.map(([c]) => c.input)).toEqual([
      expect.objectContaining({ memoryId: "mem-1", actorId: "strategist", sessionId: "daily" }),
      expect.objectContaining({ memoryId: "mem-1", actorId: "strategist", sessionId: "on-demand" }),
    ]);
  });
});

describe("designNow", () => {
  it("invokes the runtime with the prompt and returns the design", async () => {
    const { client, send } = clientReturning({
      response: { transformToString: async () => JSON.stringify({ design: DESIGN }) },
    });

    const design = await designNow({ client, config: CONFIG, prompt: "Email triage" });

    expect(design).toEqual(DESIGN);
    const command = send.mock.calls[0][0];
    expect(command).toBeInstanceOf(InvokeAgentRuntimeCommand);
    expect(command.input.agentRuntimeArn).toBe(CONFIG.runtimeArn);
    expect(JSON.parse(new TextDecoder().decode(command.input.payload))).toEqual({
      prompt: "Email triage",
    });
  });
});

describe("readPlan", () => {
  it("asks the runtime for the plan instead of a design", async () => {
    const { client, send } = clientReturning({
      response: { transformToString: async () => JSON.stringify({ plan: "# Plan" }) },
    });

    expect(await readPlan({ client, config: CONFIG })).toBe("# Plan");
    const command = send.mock.calls[0][0];
    expect(command).toBeInstanceOf(InvokeAgentRuntimeCommand);
    expect(JSON.parse(new TextDecoder().decode(command.input.payload))).toEqual({
      action: "plan",
    });
  });
});

describe("fileDesignAsIssue", () => {
  it("asks Coordinator to file the design and returns its reply", async () => {
    const { client, send } = clientReturning({
      response: {
        transformToString: async () =>
          JSON.stringify({ reply: "Created #23: https://github.com/octocat/juhta/issues/23" }),
      },
    });
    const record = {
      request: "Email triage",
      design: DESIGN,
      createdAt: new Date("2026-10-06T08:48:27Z"),
      source: "on-demand" as const,
    };

    const reply = await fileDesignAsIssue({ client, config: CONFIG, record });

    expect(reply).toBe("Created #23: https://github.com/octocat/juhta/issues/23");
    const command = send.mock.calls[0][0];
    expect(command.input.agentRuntimeArn).toBe(CONFIG.coordinatorRuntimeArn);
    const payload = JSON.parse(new TextDecoder().decode(command.input.payload));
    expect(payload.session_id).toMatch(/^dashboard-/);
    expect(payload.prompt).toContain("GitHub issue");
    expect(payload.prompt).toContain(JSON.stringify(DESIGN));
  });
});
