import { type FormEvent, useEffect, useState } from "react";
import { AgentsSection } from "./AgentsSection";
import { resumeSession, signIn, signOut } from "./auth";
import { PlanView } from "./PlanView";
import {
  type Clients,
  type Config,
  type DesignRecord,
  designNow,
  fileDesignAsIssue,
  listDesigns,
  type Source,
} from "./strategist";

const SOURCE_LABELS: Record<Source, string> = { daily: "Daily", "on-demand": "On demand" };
type Filter = Source | "all";

export function App({ config }: { config: Config }) {
  const [clients, setClients] = useState<Clients | null | undefined>(undefined);
  useEffect(() => {
    resumeSession({ config }).then(setClients);
  }, [config]);
  if (clients === undefined) return null;
  if (!clients) return <Login config={config} onSignedIn={setClients} />;
  // "All designs" opens in a new tab sharing the Amplify session.
  const allDesigns = window.location.hash === "#designs";

  async function handleSignOut(): Promise<void> {
    try {
      await signOut();
      setClients(null);
    } catch (e) {
      console.error("Sign out failed:", e);
    }
  }

  return (
    <>
      <header>
        <div>
          <a className="brand" href="/">
            <img src="/icon.png" alt="" />
            Juhta
          </a>
          {allDesigns && <span className="muted">All designs</span>}
          <button type="button" className="sign-out" onClick={handleSignOut}>
            Sign out
          </button>
        </div>
      </header>
      <main>
        {allDesigns ? (
          <Designs config={config} clients={clients} />
        ) : (
          <Home config={config} clients={clients} />
        )}
      </main>
    </>
  );
}

function Home({ config, clients }: { config: Config; clients: Clients }) {
  const [designs, setDesigns] = useState<DesignRecord[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    listDesigns({ client: clients.agentCore, config })
      .then(setDesigns)
      .catch((e) => setError(String(e)));
  }, [clients, config]);

  const latest = designs?.[0];
  return (
    <>
      <AgentsSection config={config} clients={clients} />
      <hr />
      <section>
        <div className="section-head">
          <h2>Latest design · Plan</h2>
          <a href="#designs" target="_blank" rel="noreferrer">
            All designs ↗
          </a>
        </div>
        <div className="grid">
          {latest ? (
            <Design record={latest} config={config} clients={clients} />
          ) : (
            <p className={error ? "card error" : "card muted"}>
              {error || (designs ? "No designs yet." : "Loading…")}
            </p>
          )}
          <PlanView config={config} client={clients.agentCore} />
        </div>
      </section>
    </>
  );
}

function Login({ config, onSignedIn }: { config: Config; onSignedIn: (clients: Clients) => void }) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy(true);
    setError("");
    try {
      onSignedIn(
        await signIn({
          config,
          email: String(form.get("email")),
          password: String(form.get("password")),
        }),
      );
    } catch (e) {
      setError(String(e));
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <form onSubmit={submit}>
        <img className="logo" src="/logo.png" alt="Juhta" />
        <p className="muted">Sign in to your agents</p>
        <input name="email" type="email" placeholder="Email" autoComplete="username" required />
        <input
          name="password"
          type="password"
          placeholder="Password"
          autoComplete="current-password"
          required
        />
        <button type="submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
        {error && <p className="error">{error}</p>}
      </form>
    </div>
  );
}

function Designs({ config, clients }: { config: Config; clients: Clients }) {
  const client = clients.agentCore;
  const [designs, setDesigns] = useState<DesignRecord[]>([]);
  const [filter, setFilter] = useState<Filter>("all");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    listDesigns({ client, config })
      .then(setDesigns)
      .catch((e) => setError(String(e)));
  }, [client, config]);

  const shown = filter === "all" ? designs : designs.filter((d) => d.source === filter);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    setBusy(true);
    setError("");
    try {
      await designNow({ client, config, prompt: String(new FormData(form).get("prompt")) });
      form.reset();
      setDesigns(await listDesigns({ client, config }));
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <form className="card compose" onSubmit={submit}>
        <textarea name="prompt" placeholder="Describe a workflow to automate…" required />
        {error && <p className="error">{error}</p>}
        <button type="submit" disabled={busy}>
          {busy ? "Designing…" : "Design now"}
        </button>
      </form>
      <fieldset className="range" aria-label="Filter by source">
        {(["all", "daily", "on-demand"] as const).map((key) => (
          <button
            key={key}
            type="button"
            className="toggle"
            aria-pressed={filter === key}
            onClick={() => setFilter(key)}
          >
            {key === "all" ? "All" : SOURCE_LABELS[key]} (
            {key === "all" ? designs.length : designs.filter((d) => d.source === key).length})
          </button>
        ))}
      </fieldset>
      {shown.length === 0 && <p className="muted empty">No designs yet.</p>}
      <div className="grid">
        {shown.map((record) => (
          <Design
            key={record.createdAt.toISOString()}
            record={record}
            config={config}
            clients={clients}
          />
        ))}
      </div>
    </>
  );
}

const ISSUE_URL = /https:\/\/github\.com\/[^\s>|]+\/issues\/(\d+)/;

function Design({
  record,
  config,
  clients,
}: {
  record: DesignRecord;
  config: Config;
  clients: Clients;
}) {
  const { design } = record;
  const [filing, setFiling] = useState(false);
  const [reply, setReply] = useState("");

  async function createIssue() {
    setFiling(true);
    try {
      setReply(await fileDesignAsIssue({ client: clients.agentCore, config, record }));
    } catch (e) {
      setReply(String(e));
    } finally {
      setFiling(false);
    }
  }

  const issue = reply.match(ISSUE_URL);
  return (
    <article className="card design">
      <div className="title">
        <h2>{design.name}</h2>
        <span className="badges">
          <span className={`source ${record.source}`}>{SOURCE_LABELS[record.source]}</span>
          <span className={`effort ${design.implementation_effort}`}>
            {design.implementation_effort} effort
          </span>
        </span>
      </div>
      <p className="meta muted">
        {design.role} · {record.createdAt.toLocaleString()}
      </p>
      <p className="plan-step">{design.plan_step}</p>
      <p>{design.description}</p>
      <ul className="chips">
        {design.capabilities.map((capability) => (
          <li key={capability}>{capability}</li>
        ))}
      </ul>
      <details>
        <summary>Request and system prompt</summary>
        <p className="muted">{record.request}</p>
        <pre>{design.system_prompt}</pre>
      </details>
      <div className="issue">
        {issue ? (
          <a href={issue[0]} target="_blank" rel="noreferrer">
            Issue #{issue[1]}
          </a>
        ) : (
          <button type="button" className="toggle" disabled={filing} onClick={createIssue}>
            {filing ? "Creating issue…" : "Create issue"}
          </button>
        )}
        {reply && <p className="muted">{reply}</p>}
      </div>
    </article>
  );
}
