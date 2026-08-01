import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: "http://127.0.0.1:3000",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    video: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: {
    command: "npm run dev -- --hostname 127.0.0.1",
    url: "http://127.0.0.1:3000/assistant",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    env: {
      AGENT_INTERNAL_URL: "http://127.0.0.1:18080",
      WEB_APP_ENV: "local",
      WEB_PUBLIC_ORIGIN: "http://127.0.0.1:3000",
      AGENT_BFF_SHARED_SECRET: "e2e-bff-shared-secret-not-for-production-0001",
      AGENT_ANONYMOUS_SESSION_SECRET: "e2e-anonymous-session-secret-not-for-production-0002",
    },
  },
});
