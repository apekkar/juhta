# Juhta plan

Revised 2026-10-08. The roadmap for Juhta's agents: what exists, how they work together, and
what to build next. Strategist reads this on every design; Coordinator reads it to pick "the
next step". Keep it current: a PR that finishes a step moves it from Next to Done.

## Direction

- **Agents act; CI checks.** Agents are entities that notice events, decide and talk to each
  other. GitHub Actions only runs checks and deploys; it doesn't run agents or post to Slack.
- **Code drives, the model decides within it.** Deterministic steps (git, checks, labels,
  messages) are code; the model reads, writes and chooses.
- **Coordinator is the one voice.** It is the only agent that talks to the user (Slack DMs).
- **Everything in our AWS account:** AgentCore Runtime (one microVM per session), IAM role and
  Secrets Manager secret per agent, Bedrock models, traces in CloudWatch.

## Agents

- **Coordinator:** Slack DMs (and CLI); one Memory session per user conversation. Routes to
  Strategist (design, plan, recent designs), files issues from Strategist's designs (GitHub
  MCP; it may only create issues), and on the user's explicit go-ahead `queue_issue`: labels
  the issue `queued` and hands it to the Worker. Daily at 07:00 it offers Strategist's design
  in the user's conversation; the user's "go" files it and starts the Worker in one step.
- **Strategist:** designs agents and automations that fit this plan; reads the repo
  (read-only); remembers every design (Memory, `daily` / `on-demand`).
- **Worker:** implements one issue per run (FastAPI app on AgentCore, a Strands agent inside a
  fixed code loop): clone → model edits files with tools confined to the clone (no shell, no
  `.git` / `.github`) → repo checks with up to 3 fix rounds → push a `worker/…` branch → draft
  PR (`Closes #N`) → report to Coordinator. It can't merge or deploy.

## How they talk

- One envelope between agents: `{"message": {"from": "<agent>", "text": "..."}}` on the
  receiving agent's Runtime (`POST /invocations`).
- Coordinator → Worker: `implement #N`. The Worker answers "Started on #N." and works in the
  background; the Runtime reports `HealthyBusy` meanwhile.
- Worker → Coordinator: the report. Coordinator reads agent messages as read-only turns in the
  user's Slack conversation and DMs the user.
- Coordinator → Strategist: plain routes (`prompt`, `plan`, `designs`, `daily`).
- Finding each other: stack references where they don't form a cycle; Coordinator's ARN is in
  SSM (`/juhta/runtimes/coordinator`) for agents that report to it.

## Interfaces and access

- **Slack:** DMs only, from `SLACK_USER_ID`. Events API → Coordinator's receiver Lambda
  (Function URL: HMAC check, ack, async self-invoke) → Coordinator; replies with the bot token.
- **GitHub:** every agent uses the official GitHub MCP server (stdio, in its container),
  filtered to the tools it needs; git itself only for clone/push. One fine-grained PAT per agent
  in `juhta/<agent>`:
  - Strategist: Contents read, Issues read.
  - Coordinator: Issues write (create, label); Contents and Pull requests write (merge).
  - Worker: Contents and Pull requests write, Issues read. No Workflows permission.
- **Dashboard:** one page with per-agent status (ping), token usage and cost, and recent
  activity from the agents' traces; latest design next to this plan.
- **CI/CD:** PR checks (lint, types, unit and integration tests, security checks, and the
  Worker against the real model); merge to `main` deploys with `cdk deploy --all`.

## Build order

Done:

- Strategist: reads the repo and this plan; designs stored per source and shown in the
  dashboard.
- Coordinator: AgentCore Runtime + Memory, Strategist as tools, two-way Slack through the
  receiver Lambda, the 07:00 daily run.
- Coordinator files issues from Strategist's designs (matched by plan step; duplicates
  checked).
- Dashboard as one page: agent status, usage and cost, activity, latest design and plan.
- Agent messaging: the envelope; Coordinator reads agent messages read-only in the user's
  conversation.
- Worker agent (`worker/`, `JuhtaWorkerStack`): issue → draft PR → report; unit, integration and
  real-model tests, the last also in PR CI.
- Coordinator hands queued issues to the Worker (`queue_issue`); stacks without a cycle (SSM).
- GitHub events → Coordinator: a repository webhook to the receiver Lambda (GitHub's HMAC
  checked); finished CI runs (a PR's checks, the deploy) arrive as messages from "github",
  read-only turns in the user's conversation. Other events are dropped in the receiver.
- Worker addresses reviews: the user's "changes requested" review on a Worker PR reaches
  Coordinator through the GitHub webhook; Coordinator's `address_review` sends the Worker
  `address review on PR #N`; the Worker reads the review and open inline comments (GitHub
  MCP), edits on the PR's own `worker/` branch (never anyone else's), runs the checks with fix
  rounds, pushes and reports.
- One "go" per idea: the daily design is offered in the user's conversation (a read-only
  turn); answering "go" files the issue and queues it for the Worker in one turn.
- Merge on approval: the user's approval reaches Coordinator through the webhook; it asks
  "merge #N?" and on yes `merge_pr` merges (rebase) — in code, only if the PR is open, not a
  draft, the user's latest review approves it and every check passed; never in event or agent
  turns. Coordinator's PAT has Contents and Pull requests write for it.
- Strategist and Worker use Claude Sonnet 4.6 through the EU Bedrock inference profile.

Next:

1. **Bot GitHub account** with one fine-grained PAT per agent, so agent commits and PRs aren't
   the user's.
2. **GitHub Project board** over the issues (`queued` label exists). No code.

## Later / open

- Worker quality: if real PRs fall short, add a reviewer step (Strands Graph: implement →
  review → back) or run Claude Agent SDK's loop inside the same container.
- Strategist and Coordinator on the Worker's FastAPI app pattern, one at a time.
- A GitHub App instead of PATs if this grows past one repo.
- Dropped: Actions-run agents (claude-code-action), PR summaries from Actions, Incoming
  Webhooks, the weekly "Steward" agent (Strategist covers it).
