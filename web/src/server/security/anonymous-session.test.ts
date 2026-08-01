import { describe, expect, it } from "vitest";

import {
  ANONYMOUS_SESSION_COOKIE_NAME,
  HOSTED_ANONYMOUS_SESSION_COOKIE_NAME,
  loadAnonymousSessionConfig,
  resolveAnonymousSession,
  verifyCookieToken,
} from "./anonymous-session";

const SESSION_SECRET = "anonymous-session-secret-".padEnd(48, "s");

describe("anonymous session configuration", () => {
  it("requires an independent secret with at least 32 UTF-8 bytes", () => {
    expect(() => loadAnonymousSessionConfig({ WEB_APP_ENV: "local" })).toThrow();
    expect(() => loadAnonymousSessionConfig({
      AGENT_ANONYMOUS_SESSION_SECRET: "short",
      WEB_APP_ENV: "local",
    })).toThrow();
    expect(() => loadAnonymousSessionConfig({
      AGENT_ANONYMOUS_SESSION_SECRET: ` ${SESSION_SECRET}`,
      WEB_APP_ENV: "local",
    })).toThrow();
    expect(() => loadAnonymousSessionConfig({
      AGENT_ANONYMOUS_SESSION_SECRET: SESSION_SECRET,
      WEB_APP_ENV: "preview",
    })).toThrow();
    expect(() => loadAnonymousSessionConfig({
      AGENT_ANONYMOUS_SESSION_SECRET: SESSION_SECRET,
      NODE_ENV: "production",
    })).toThrow();
    expect(() => loadAnonymousSessionConfig({
      AGENT_ANONYMOUS_SESSION_SECRET: SESSION_SECRET,
      WEB_APP_ENV: "production",
    })).toThrow();
  });

  it("enables Secure for staging and production but not local", () => {
    expect(loadAnonymousSessionConfig({
      AGENT_ANONYMOUS_SESSION_SECRET: SESSION_SECRET,
      WEB_APP_ENV: "local",
    }).secure).toBe(false);
    expect(loadAnonymousSessionConfig({
      AGENT_ANONYMOUS_SESSION_SECRET: SESSION_SECRET,
      WEB_APP_ENV: "staging",
      WEB_PUBLIC_ORIGIN: "https://web.test",
    }).secure).toBe(true);
    expect(loadAnonymousSessionConfig({
      AGENT_ANONYMOUS_SESSION_SECRET: SESSION_SECRET,
      WEB_APP_ENV: "production",
      WEB_PUBLIC_ORIGIN: "https://web.test",
    }).secure).toBe(true);
  });

  it.each([
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:4317",
  ])("accepts an explicit local HTTP public Origin %s", (publicOrigin) => {
    expect(loadAnonymousSessionConfig({
      AGENT_ANONYMOUS_SESSION_SECRET: SESSION_SECRET,
      WEB_APP_ENV: "local",
      WEB_PUBLIC_ORIGIN: publicOrigin,
    }).publicOrigin).toBe(publicOrigin);
  });
});

describe("signed anonymous session cookie", () => {
  it("creates an HttpOnly Lax cookie that does not expose the owner id", () => {
    const config = loadAnonymousSessionConfig({
      AGENT_ANONYMOUS_SESSION_SECRET: SESSION_SECRET,
      WEB_APP_ENV: "local",
    });
    const session = resolveAnonymousSession(
      new Request("http://web.test/api/agent/readiness"),
      config,
    );

    expect(session.ownerId).toMatch(/^[a-f0-9]{64}$/);
    expect(session.setCookie).toContain(`${ANONYMOUS_SESSION_COOKIE_NAME}=v1.`);
    expect(session.setCookie).toContain("Path=/");
    expect(session.setCookie).toContain("HttpOnly");
    expect(session.setCookie).toContain("SameSite=Lax");
    expect(session.setCookie).not.toContain("Secure");
    expect(session.setCookie).not.toContain(session.ownerId);
  });

  it("adds Secure in production and reuses a valid signed cookie", () => {
    const config = loadAnonymousSessionConfig({
      AGENT_ANONYMOUS_SESSION_SECRET: SESSION_SECRET,
      WEB_APP_ENV: "production",
      WEB_PUBLIC_ORIGIN: "https://web.test",
    });
    const first = resolveAnonymousSession(
      new Request("https://web.test/api/agent/readiness"),
      config,
    );
    const cookiePair = first.setCookie?.split(";", 1)[0];
    expect(first.setCookie).toContain("Secure");
    expect(first.setCookie).toContain(`${HOSTED_ANONYMOUS_SESSION_COOKIE_NAME}=v1.`);
    expect(cookiePair).toBeDefined();

    const second = resolveAnonymousSession(
      new Request("https://web.test/api/agent/readiness", {
        headers: { cookie: cookiePair ?? "" },
      }),
      config,
    );

    expect(second.ownerId).toBe(first.ownerId);
    expect(second.setCookie).toBeNull();
  });

  it("rejects a same-length tampered signature and rotates the session", () => {
    const config = loadAnonymousSessionConfig({
      AGENT_ANONYMOUS_SESSION_SECRET: SESSION_SECRET,
      WEB_APP_ENV: "local",
    });
    const first = resolveAnonymousSession(
      new Request("http://web.test/api/agent/readiness"),
      config,
    );
    const token = first.setCookie?.split(";", 1)[0]?.split("=", 2)[1];
    expect(token).toBeDefined();
    const replacement = token?.endsWith("A") === true ? "B" : "A";
    const tampered = `${token?.slice(0, -1)}${replacement}`;

    expect(verifyCookieToken(tampered, SESSION_SECRET)).toBeNull();
    const rotated = resolveAnonymousSession(
      new Request("http://web.test/api/agent/readiness", {
        headers: { cookie: `${ANONYMOUS_SESSION_COOKIE_NAME}=${tampered}` },
      }),
      config,
    );
    expect(rotated.ownerId).not.toBe(first.ownerId);
    expect(rotated.setCookie).not.toBeNull();
  });

  it("rejects ambiguous duplicate session cookies", () => {
    const config = loadAnonymousSessionConfig({
      AGENT_ANONYMOUS_SESSION_SECRET: SESSION_SECRET,
      WEB_APP_ENV: "local",
    });
    const first = resolveAnonymousSession(
      new Request("http://web.test/api/agent/readiness"),
      config,
    );
    const cookiePair = first.setCookie?.split(";", 1)[0] ?? "";
    const rotated = resolveAnonymousSession(
      new Request("http://web.test/api/agent/readiness", {
        headers: { cookie: `${cookiePair}; ${cookiePair}` },
      }),
      config,
    );

    expect(rotated.ownerId).not.toBe(first.ownerId);
    expect(rotated.setCookie).not.toBeNull();
  });
});
