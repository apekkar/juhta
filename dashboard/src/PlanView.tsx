import type { BedrockAgentCoreClient } from "@aws-sdk/client-bedrock-agentcore";
import { useEffect, useState } from "react";
import Markdown from "react-markdown";
import { type Config, readPlan } from "./strategist";

export function PlanView({ config, client }: { config: Config; client: BedrockAgentCoreClient }) {
  const [plan, setPlan] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    readPlan({ client, config })
      .then(setPlan)
      .catch((e) => setError(String(e)));
  }, [client, config]);

  if (error) return <p className="error">{error}</p>;
  return (
    <article className="card plan">
      <Markdown>{plan}</Markdown>
    </article>
  );
}
