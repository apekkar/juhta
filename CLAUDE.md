# CLAUDE.md — Juhta

## Project Overview

Personal AWS-native agents for my own workflows, not a framework.
- **Strategist** (built): designs agents that fit the plan, on demand and daily.
  Strands on Amazon Bedrock, deployed to AgentCore Runtime, history in AgentCore Memory.
- **Coordinator** (built): the one agent you talk to (Slack, CLI); routes to the others.
- **Worker** (built): Strands agent that implements an issue and opens a draft PR, then reports
  to Coordinator. Coordinator's `queue_issue` starts it.
- Roadmap: `docs/agent-pipeline-plan.md`.

## AWS

- Profile: your SSO profile (`export AWS_PROFILE=<your-profile>`)
- Region: `eu-west-1`. Model: `eu.anthropic.claude-sonnet-4-6`
- Config: see Environment Variables below.

## Environment Variables

- **One `.env` at the repo root** for everything (CDK, containers, scripts). No per-agent `.env` files.
- Categorize with section headers per area: `# ===== AWS =====`, `# ===== Strategist =====`, `# ===== Coordinator =====`.
- `.env.example` mirrors `.env` with safe values; update both together. Never commit `.env`.
- Python loads it with `python-dotenv` (`load_dotenv(...)`), never a hand-written parser.
  Read values with `os.environ["KEY"]`.
- Containers: `docker run --env-file .env`. Shell scripts: `set -a; source .env; set +a`.
- No AWS credentials in `.env`: export them from the SSO session instead.
- Tokens and other secrets: **one Secrets Manager secret per agent**, `juhta/<agent>`, holding a
  JSON object with all that agent's values (e.g. `{"github_pat": ...}`). CDK creates it with a
  placeholder and grants only that agent's role; values are set by hand. The agent reads it at
  startup via `AGENT_SECRET_ARN`. Never in `.env`, CDK env vars or templates.

## Documentation

- **One README, at the repo root.** No READMEs in subfolders (`cdk/`, `strategist/`, ...).
- Short and concise, with a table of contents at the top.
- New agents or components get a section in the root README, not their own file.
- Rationale for a specific design choice goes in the module docstring next to the code, not in a separate doc.
- **One plan file: `docs/agent-pipeline-plan.md`** — the roadmap Strategist reads on every design and
  the dashboard's Plan view shows. Keep it current; no other spec/plan files or `docs/` content.
  A PR that finishes a build-order step moves it from Next to Done in the same PR: agents read
  Next literally and will re-propose finished work otherwise.

### No generated output files in the repo

- Never write summaries, reports, extra plans, or help files (e.g. `CLEANUP_SUMMARY.md`, `Claude outputs/`) into the repo.
- Temporary files go to the session scratchpad, outside the repo.
- Results are reported in the reply. Anything durable goes in the README, a docstring, or the commit message.

## Python Package Management (CRITICAL)

- **MUST use `uv`**, not pip: `uv add`, `uv remove`, `uv sync`, `uv run <cmd>`
- CDK commands: `uv run --directory cdk cdk <command>`

## CDK Infrastructure Structure

- `cdk/app.py`: entrypoint, loads `.env`
- `cdk/strategist_stack.py`: `JuhtaStrategistStack` (Memory, Runtime, execution role, Bedrock IAM)
- `cdk/coordinator_stack.py`: `JuhtaCoordinatorStack` (Memory, Runtime, Slack token secret; may invoke Strategist and the Worker; publishes its ARN in SSM)
- `cdk/worker_stack.py`: `JuhtaWorkerStack` (Runtime, `juhta/worker` PAT secret; may invoke Coordinator, found via SSM)
- `cdk/dashboard_stack.py`: `JuhtaDashboardStack` (S3 + CloudFront, Cognito, identity pool role)
- New agents get their own stack file: `cdk/<agent>_stack.py`

## Code Standards (project)

### Lint, format, type check

Run before calling work done; all must pass:

```bash
uv run ruff check --fix . && uv run ruff format . && uv run mypy && uv run pytest
(cd dashboard && npm run format && npm run typecheck && npm test)
```

Rules are configured in `pyproject.toml` (`[tool.ruff.lint]`, `[tool.mypy]` strict) and
`dashboard/biome.jsonc` (Biome: lint + format, the TypeScript counterpart of Ruff).

### No over-guarding — fail fast

Don't hand-write checks with custom error messages for config or parameters you own.
Make the parameter required and let the runtime fail.

Avoid:

```python
if not agentcore_memory_id:
    raise ValueError("AGENTCORE_MEMORY_ID is not set in .env — run ...")
```

Do instead:
- Required keyword-only parameter, no default, no `Optional`:
  `def __init__(self, scope, id, *, agentcore_memory_id: str, ...)`
- Required config: `os.environ["AGENTCORE_MEMORY_ID"]` (the `KeyError` names the missing key),
  not `os.environ.get(...)` followed by `raise`.
- `.get()` with a default only for values that are truly optional.
- Shell scripts: rely on `set -euo pipefail`, not manual file/variable checks.

Validating external input (e.g. HTTP request payloads) is not over-guarding.

## Testing Strategy

- Tests live next to their component: `strategist/tests/`, `coordinator/tests/`,
  `coordinator_receiver/tests/`, `worker/tests/`, `cdk/tests/`,
  `dashboard/src/*.test.ts`.
  Python: `uv run pytest`
- AWS clients (Bedrock, AgentCore Memory) mocked at the boundary
- Fakes for external APIs (GitHub MCP server, AWS, Slack) return **recorded real responses**,
  never guessed shapes: record one from the real service first. Green tests on invented shapes
  hide broken integrations.
- Worker: integration tests through its FastAPI app (`worker/tests/integration/`) with real git
  (a local bare repo as GitHub) and fakes only at the boundaries (`worker/tests/fakes.py`:
  scripted model, GitHub MCP server, AgentCore), and an integration test with the real model (`uv run pytest -m bedrock`; real
  Bedrock model and repo checks; env and secrets via `worker/tests/conftest.py`, from Secrets
  Manager locally, a dummy in CI; not in the default `pytest` run, runs in PR CI)
- CDK stacks tested with `aws_cdk.assertions.Template`
- Dashboard: vitest in `dashboard/src/*.test.ts`, AWS SDK client mocked (`send`)
- After unit tests pass, check the real thing: the README's local container test, then the
  README's smoke test (`aws bedrock-agentcore invoke-agent-runtime`) after deploy

## CI/CD & Git workflow

- **Trunk-based, PR-only.** Never commit to `main` directly: branch, push, open a PR into `main`.
- **PR:** `on-pull-request` runs, in parallel, `python/style-checks` (ruff, mypy),
  `python/unit-tests` (pytest), `dashboard/style-checks` (Biome, tsc) and `dashboard/unit-tests`
  (vitest), then `worker/bedrock-tests` after the unit tests. All must pass before merge.
- **Merge to `main`:** `on-push-to-main` runs the same checks, then the `deploy` action (dashboard
  build + `cdk deploy --all`; one CDK app, so one deploy) via the GitHub OIDC
  role from `JuhtaGithubStack`. No AWS keys in GitHub.
- **Security checks** (`security-checks`, parallel on every PR and before deploy):
  - **cdk-nag** (AWS Solutions rules) runs on every synth (`cdk/app.py`). Fix findings; only if
    a finding is an accepted exception, acknowledge it next to the construct with
    `nag.acknowledge(construct, {"<exact finding id>": "<reason>"})`.
  - **zizmor** on `.github/` (medium+ fails): pin actions to commit SHAs (`@<sha> # vX.Y.Z`),
    workflow `permissions: contents: read`, `id-token: write` only on jobs that need AWS.
  - **pip-audit** and **npm audit** (high+) on dependencies.
- Toolchains and dependencies come from one action, `setup-env` (inputs `python`, `node`; uv and
  npm caches). Actions call it instead of repeating setup-uv/setup-node/uv sync/npm ci.
- Logic lives in composite actions, grouped per component (`.github/actions/<component>/<name>/action.yml`); workflows are named after
  their trigger (`on-<event>.yml`) and only call actions.
- CI config lives in GitHub repository variables, mirroring `.env`.
- New stacks: add to `cdk/app.py`; `--all` picks them up. No wrapper scripts.
- Manual deploys only for the one-time bootstrap (`cdk bootstrap`, first `cdk deploy --all`).
- Local container needs SSO creds in shell:
  `eval "$(aws configure export-credentials --profile <your-profile> --format env)"`
