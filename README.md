<p align="center"><img src="dashboard/public/logo.png" alt="Juhta" width="120" /></p>

---

Personal AWS-native agents for my own workflows, on Amazon Bedrock AgentCore:
**Coordinator** (the one you talk to, in Slack), **Strategist** (designs agents that fit the
plan) and **Worker** (implements an issue and opens a draft PR).

- [Setup](#setup)
- [Deploy](#deploy)
- [Strategist](#strategist)
- [Coordinator](#coordinator)
- [Worker](#worker)
- [Dashboard](#dashboard)
- [Local test](#local-test)
- [Project structure](#project-structure)

## Setup

Prerequisites: [uv](https://docs.astral.sh/uv/), Docker, AWS CLI with an SSO profile, Bedrock
model access, Node.js and the CDK CLI (`npm install -g aws-cdk`).

```bash
cp .env.example .env          # one env file for the whole repo; fill in values
aws sso login --profile <your-profile>
eval "$(aws configure export-credentials --profile <your-profile> --format env)"
```

`BEDROCK_MODEL_ID` must be a cross-region inference profile (`eu.` / `us.` / `global.` prefix).

## Deploy

AWS CDK (`cdk/`). Merges to `main` deploy via GitHub Actions (OIDC, no stored keys; repository
variables mirror `.env` plus the role ARNs output by `JuhtaGithubStack`). The first deploy is manual:

```bash
export AWS_PROFILE=<your-profile>
uv sync
(cd dashboard && npm ci && npm run build)
uv run --directory cdk cdk bootstrap
uv run --directory cdk cdk deploy --all
```

| Stack | Contents |
|---|---|
| `JuhtaStrategistStack` | Memory, Runtime, secret `juhta/strategist` |
| `JuhtaCoordinatorStack` | Memory, Runtime, secret `juhta/coordinator`, receiver Lambda + Function URL (`SlackEventsUrl`), daily schedule |
| `JuhtaWorkerStack` | Runtime, secret `juhta/worker` |
| `JuhtaDashboardStack` | S3 + CloudFront, Cognito, identity pool role |
| `JuhtaGithubStack` | GitHub OIDC provider and CI roles |

Each agent's secret is one JSON object, set by hand after the first deploy. GitHub tokens are
fine-grained PATs scoped to this repo.

`cdk destroy` retains Strategist's Memory; delete it before redeploying, or the name conflicts.

## Strategist

```
{"prompt": "workflow description"}  → design (reads the plan, the repo and earlier designs)
{"action": "plan"}                  → {"plan": "..."} (docs/agent-pipeline-plan.md, no model call)
{"action": "designs", "limit": 10}  → newest designs from Memory
{"action": "ping"}                  → {"status": "ok"}
{"action": "daily"}                 → design for the next plan step (Coordinator's 07:00 run)
```

PAT: Contents and Issues read-only.

```bash
aws secretsmanager put-secret-value --secret-id juhta/strategist --secret-string '{"github_pat": "<github_pat_...>"}'
aws bedrock-agentcore invoke-agent-runtime \
  --agent-runtime-arn "$(aws cloudformation describe-stacks --stack-name JuhtaStrategistStack --query "Stacks[0].Outputs[?OutputKey=='StrategistRuntimeArn'].OutputValue" --output text)" \
  --runtime-session-id "$(uuidgen)" --cli-binary-format raw-in-base64-out \
  --payload '{"prompt": "Email processing workflow"}' \
  /tmp/strategist-response.json && jq . /tmp/strategist-response.json
```

## Coordinator

DM it in Slack or use the CLI. It routes to Strategist, files issues from designs, queues them
for the Worker ("go" / "queue #N"), passes review feedback to the Worker and merges approved PRs
after your yes. Daily at 07:00 it offers Strategist's design; "go" files it and starts the Worker.

```
Slack DM → receiver Lambda → Coordinator Runtime → reply in the DM
07:00    → EventBridge Scheduler → receiver Lambda → Coordinator → Strategist daily design → DM
GitHub   → repository webhook → receiver Lambda → Coordinator → DM
```

Slack setup (one-time):

1. Create an app at <https://api.slack.com/apps>, bot scopes `chat:write`, `im:history`, install it.
2. Store the secrets (PAT: Issues, Contents and Pull requests read and write):

   ```bash
   aws secretsmanager put-secret-value --secret-id juhta/coordinator \
     --secret-string '{"slack_bot_token": "xoxb-...", "slack_signing_secret": "...", "github_pat": "github_pat_...", "github_webhook_secret": "<openssl rand -hex 32>"}'
   ```

3. **Event Subscriptions** → Request URL: `SlackEventsUrl` → bot event `message.im`.
4. **App Home** → Messages tab on, "Allow users to send messages" on.
5. Your Slack member ID in `.env` as `SLACK_USER_ID`.

GitHub webhook: Payload URL `SlackEventsUrl`, content type `application/json`, secret
`github_webhook_secret`, events **Workflow runs** and **Pull request reviews**.

```bash
ARN=$(aws cloudformation describe-stacks --stack-name JuhtaCoordinatorStack --query "Stacks[0].Outputs[?OutputKey=='CoordinatorRuntimeArn'].OutputValue" --output text)
aws bedrock-agentcore invoke-agent-runtime --agent-runtime-arn "$ARN" \
  --runtime-session-id "$(uuidgen)" --cli-binary-format raw-in-base64-out \
  --payload '{"prompt": "What should we build next?", "session_id": "cli-1"}' \
  /tmp/coordinator.json && jq -r .reply /tmp/coordinator.json
```

## Worker

FastAPI app with a Strands agent (`worker/`). On `implement #N` from Coordinator it clones the
repo, lets the model edit files, runs the repo's checks (up to 3 fix rounds), pushes a
`worker/…` branch, opens a draft PR and reports back. On `address review on PR #N` it does the
same on that PR's branch.

PAT in `juhta/worker` as `{"github_pat": "..."}`: Contents and Pull requests read and write,
Issues read. Protect `main`.

```bash
aws bedrock-agentcore invoke-agent-runtime --agent-runtime-arn "$WORKER_ARN" \
  --runtime-session-id "worker-cli-$(uuidgen)" \
  --payload "$(echo -n '{"message":{"from":"coordinator","text":"implement #N"}}' | base64)" out.json
```

Real-model test: `uv run pytest -m bedrock`.

## Dashboard

React (Vite) app in `dashboard/`: agent status, usage and cost, activity, latest design next to
the plan, and all designs. No backend: Cognito login, then the browser calls AWS directly.
Create your user once:

```bash
POOL=$(aws cloudformation describe-stacks --stack-name JuhtaDashboardStack --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" --output text)
aws cognito-idp admin-create-user --user-pool-id "$POOL" --username you@example.com --message-action SUPPRESS
aws cognito-idp admin-set-user-password --user-pool-id "$POOL" --username you@example.com --password '<password>' --permanent
```

Open the `DashboardUrl` output. Local dev: copy the deployed `config.json` into
`dashboard/public/`, then `cd dashboard && npm run dev`.

## Local test

```bash
docker buildx build --platform linux/arm64 -f strategist/Dockerfile -t strategist --load .
docker run --platform linux/arm64 -p 8080:8080 --env-file .env \
  -e AGENT_SECRET_ARN=juhta/strategist \
  -e AGENTCORE_MEMORY_ID="$(aws cloudformation describe-stacks --stack-name JuhtaStrategistStack --query "Stacks[0].Outputs[?OutputKey=='StrategistMemoryId'].OutputValue" --output text)" \
  -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_SESSION_TOKEN strategist
curl -X POST localhost:8080/invocations -H "Content-Type: application/json" -d '{"prompt": "Email processing workflow"}'
```

## Project structure

```
juhta/
├── strategist/               # Strategist: agent.py, server.py, tests/, Dockerfile
├── coordinator/              # Coordinator (same layout)
├── coordinator_receiver/     # Receiver Lambda: Slack, GitHub webhook, daily schedule
├── worker/                   # Worker: app.py, routers/, agent.py, workspace.py, tests/, Dockerfile
├── dashboard/                # React (Vite) dashboard
├── cdk/                      # CDK app and stacks; tests/
├── .github/                  # Composite actions and workflows
├── docs/agent-pipeline-plan.md  # The roadmap
└── CLAUDE.md                 # Development guidelines
```
