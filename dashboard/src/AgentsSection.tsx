import { useEffect, useState } from "react";
import {
  type Activity,
  AGENTS,
  type AgentKey,
  agentStatus,
  loadActivity,
  loadUsage,
  pingAgent,
  type Status,
  type TokenUsage,
} from "./agents";
import type { Clients, Config } from "./strategist";

const STATUS_LABELS: Record<Status, string> = {
  checking: "… Checking",
  working: "● Working",
  idle: "✓ Idle",
  failure: "✕ Failure",
};
const ACTIVITY_DAYS = 7;
const SHOWN_STEPS = 5;

const compact = new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 });
const usd = new Intl.NumberFormat("en", { style: "currency", currency: "USD" });
const relative = new Intl.RelativeTimeFormat("en", { numeric: "auto" });

function ago({ at, now }: { at: Date; now: Date }): string {
  const minutes = Math.round((at.getTime() - now.getTime()) / 60_000);
  if (minutes > -60) return relative.format(minutes, "minute");
  if (minutes > -24 * 60) return relative.format(Math.round(minutes / 60), "hour");
  return relative.format(Math.round(minutes / (24 * 60)), "day");
}

type PerAgent<T> = Record<AgentKey, T>;

/** Pings once per page load: each ping is a real (model-free) invocation. */
export function AgentsSection({ config, clients }: { config: Config; clients: Clients }) {
  const [now] = useState(() => new Date());
  const [pings, setPings] = useState<Partial<PerAgent<boolean>>>({});
  const [today, setToday] = useState<PerAgent<TokenUsage> | null>(null);
  const [month, setMonth] = useState<PerAgent<TokenUsage> | null>(null);
  const [activity, setActivity] = useState<PerAgent<Activity[]> | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    const fail = (e: unknown) => setError(String(e));
    for (const agent of AGENTS) {
      pingAgent({ client: clients.agentCore, runtimeArn: agent.runtimeArn(config) }).then((ok) =>
        setPings((previous) => ({ ...previous, [agent.key]: ok })),
      );
    }
    const startOfDay = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const startOfMonth = new Date(now.getFullYear(), now.getMonth(), 1);
    const activitySince = new Date(now.getTime() - ACTIVITY_DAYS * 86_400_000);
    loadUsage({ client: clients.logs, start: startOfDay, end: now }).then(setToday).catch(fail);
    loadUsage({ client: clients.logs, start: startOfMonth, end: now }).then(setMonth).catch(fail);
    loadActivity({ client: clients.logs, start: activitySince, end: now })
      .then(setActivity)
      .catch(fail);
  }, [clients, config, now]);

  return (
    <section>
      <div className="section-head">
        <h2>Agents</h2>
      </div>
      {error && <p className="error">{error}</p>}
      <div className="grid">
        {AGENTS.map(({ key, name }) => (
          <article key={key} className="card agent">
            <div className="title">
              <h3>{name}</h3>
              <StatusBadge
                status={
                  activity
                    ? agentStatus({ ping: pings[key], activity: activity[key], now })
                    : "checking"
                }
              />
            </div>
            <dl className="usage">
              <UsageFigure label="Today" usage={today?.[key]} />
              <UsageFigure label="This month" usage={month?.[key]} />
            </dl>
            <ActivityList steps={activity?.[key]} now={now} />
          </article>
        ))}
      </div>
    </section>
  );
}

function StatusBadge({ status }: { status: Status }) {
  return <span className={`status ${status}`}>{STATUS_LABELS[status]}</span>;
}

function UsageFigure({ label, usage }: { label: string; usage: TokenUsage | undefined }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd className="figure">
        {usage ? (usage.costUsd === null ? "cost n/a" : usd.format(usage.costUsd)) : "…"}
      </dd>
      {usage && (
        <dd className="muted">
          {usage.runs} runs · {compact.format(usage.inputTokens + usage.outputTokens)} tokens
        </dd>
      )}
    </div>
  );
}

function ActivityList({ steps, now }: { steps: Activity[] | undefined; now: Date }) {
  if (!steps) return <p className="muted">Loading activity…</p>;
  if (!steps.length) return <p className="muted">No activity in the last {ACTIVITY_DAYS} days.</p>;
  return (
    <ul className="activity">
      {steps.slice(0, SHOWN_STEPS).map((step) => (
        <li key={`${step.at.toISOString()}-${step.label}`} className={step.failed ? "failed" : ""}>
          <span>{step.label}</span>
          <time dateTime={step.at.toISOString()} className="muted">
            {ago({ at: step.at, now })}
          </time>
        </li>
      ))}
    </ul>
  );
}
