import * as amplifyAuth from "aws-amplify/auth";
import { afterEach, describe, expect, it, vi } from "vitest";
import { resumeSession, signIn, signOut } from "./auth";
import type { Config } from "./strategist";

vi.mock("aws-amplify/auth");

const CONFIG = {
  region: "eu-west-1",
  userPoolId: "eu-west-1_pool",
  userPoolClientId: "client",
  identityPoolId: "eu-west-1:identity",
} as Config;

const CREDENTIALS = { accessKeyId: "AKIA", secretAccessKey: "secret", sessionToken: "token" };

afterEach(() => {
  vi.resetAllMocks();
});

describe("resumeSession", () => {
  it("returns null when nobody is signed in", async () => {
    vi.mocked(amplifyAuth.fetchAuthSession).mockResolvedValue({});

    expect(await resumeSession({ config: CONFIG })).toBeNull();
  });

  it("gives clients the signed-in session's credentials, fetched per request", async () => {
    vi.mocked(amplifyAuth.fetchAuthSession).mockResolvedValue({
      tokens: {} as amplifyAuth.AuthTokens,
      credentials: CREDENTIALS,
    });

    const clients = await resumeSession({ config: CONFIG });

    expect(await clients!.agentCore.config.credentials()).toMatchObject(CREDENTIALS);
  });
});

describe("signIn", () => {
  it("fails when Cognito asks for another step", async () => {
    vi.mocked(amplifyAuth.signIn).mockResolvedValue({
      isSignedIn: false,
      nextStep: { signInStep: "CONFIRM_SIGN_IN_WITH_NEW_PASSWORD_REQUIRED" },
    } as amplifyAuth.SignInOutput);

    await expect(
      signIn({ config: CONFIG, email: "me@example.com", password: "secret" }),
    ).rejects.toThrow("CONFIRM_SIGN_IN_WITH_NEW_PASSWORD_REQUIRED");
  });
});

describe("signOut", () => {
  it("calls Amplify signOut", async () => {
    vi.mocked(amplifyAuth.signOut).mockResolvedValue();

    await signOut();

    expect(amplifyAuth.signOut).toHaveBeenCalledOnce();
  });
});
