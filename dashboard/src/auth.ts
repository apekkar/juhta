/** Cognito sign-in via Amplify Auth, which stores, refreshes and resumes the session. */
import { BedrockAgentCoreClient } from "@aws-sdk/client-bedrock-agentcore";
import { CloudWatchLogsClient } from "@aws-sdk/client-cloudwatch-logs";
import { Amplify } from "aws-amplify";
import {
  signIn as amplifySignIn,
  signOut as amplifySignOut,
  fetchAuthSession,
} from "aws-amplify/auth";
import type { Clients, Config } from "./strategist";

export function configureAuth({ config }: { config: Config }): void {
  Amplify.configure({
    Auth: {
      Cognito: {
        userPoolId: config.userPoolId,
        userPoolClientId: config.userPoolClientId,
        identityPoolId: config.identityPoolId,
      },
    },
  });
}

export async function signIn({
  config,
  email,
  password,
}: {
  config: Config;
  email: string;
  password: string;
}): Promise<Clients> {
  const { isSignedIn, nextStep } = await amplifySignIn({ username: email, password });
  if (!isSignedIn) throw new Error(`Sign-in needs another step: ${nextStep.signInStep}`);
  return clientsFor({ config });
}

export async function resumeSession({ config }: { config: Config }): Promise<Clients | null> {
  const { tokens } = await fetchAuthSession();
  return tokens ? clientsFor({ config }) : null;
}

export async function signOut(): Promise<void> {
  await amplifySignOut();
}

function clientsFor({ config }: { config: Config }): Clients {
  // Asked per request, so Amplify can hand out refreshed credentials.
  const credentials = async () => (await fetchAuthSession()).credentials!;
  return {
    agentCore: new BedrockAgentCoreClient({ region: config.region, credentials }),
    logs: new CloudWatchLogsClient({ region: config.region, credentials }),
  };
}
