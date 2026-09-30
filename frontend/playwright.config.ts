import { defineConfig } from "@playwright/test";

/**
 * End-to-end tests against a RUNNING stack: real Django, real Celery worker,
 * real PostgreSQL and Redis, real models. Nothing here is mocked - that is
 * the point of this suite, and the unit tests under src/ cover the rest.
 *
 *   E2E_BASE_URL   the frontend origin (default http://localhost:5173)
 *   E2E_PASSWORD   password shared by the seeded e2e accounts
 *   E2E_CHANNEL    browser channel to drive: chrome (default) or msedge,
 *                  so no browser download is needed on a workstation
 *
 * See docs/development/testing.md for how the stack and accounts are
 * prepared on a disposable database.
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 240_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  outputDir: "./e2e/.results",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:5173",
    channel: process.env.E2E_CHANNEL ?? "chrome",
    headless: true,
    viewport: { width: 1360, height: 900 },
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
});
