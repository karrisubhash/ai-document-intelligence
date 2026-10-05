import { expect, test } from "@playwright/test";

import { PASSWORD } from "./fixtures";

/**
 * The Content-Security-Policy must not break the application it protects.
 *
 * A CSP violation does not fail a page load - the browser just refuses the
 * resource and logs it - so a policy that blanks the PDF preview or blocks
 * a stylesheet passes every functional test that only checks an element
 * exists. This visits each screen and fails on any violation at all.
 *
 * Meaningful only against a stack whose edge sends the policy (nginx in the
 * production image); against the Vite dev server there is no CSP to break.
 */
test.skip(!PASSWORD, "E2E_PASSWORD is not set");

test("no screen triggers a Content-Security-Policy violation", async ({ page }) => {
  const violations: string[] = [];
  page.on("console", (message) => {
    if (/Content Security Policy|Refused to/i.test(message.text())) {
      violations.push(message.text());
    }
  });
  await page.addInitScript(() => {
    document.addEventListener("securitypolicyviolation", (event) => {
      console.error(
        `Refused to load (CSP ${event.violatedDirective}): ${event.blockedURI || "inline"}`,
      );
    });
  });

  const response = await page.goto("/");
  const policy = response?.headers()["content-security-policy"] ?? "";
  test.skip(!policy, "this stack does not send a Content-Security-Policy");

  await page.getByLabel("Username").fill("admin");
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: /^Welcome,/ })).toBeVisible();

  for (const path of [
    "/documents",
    "/search",
    "/ask",
    "/account",
    "/admin/users",
    "/admin/departments",
    "/admin/processing",
    "/admin/audit",
    "/admin/system",
  ]) {
    // Loaded as a document, not through the router: each path must come
    // back as the application shell, with the policy. /admin/* once went to
    // Django's admin instead - a page with no CSP, so this loop "passed"
    // without checking the screens at all.
    const screen = await page.goto(path);
    expect(screen?.headers()["content-security-policy"], `${path} did not return the app shell`).toBe(policy);
    await page.waitForLoadState("networkidle");
  }

  // Every document's preview - the PDF (blob: frame) and the scanned image
  // (blob: img) are the two places the policy's blob: allowances exist for.
  await page.goto("/documents?archived=all");
  const links = await page.locator("a.table-title").evaluateAll((anchors) =>
    anchors.map((anchor) => (anchor as HTMLAnchorElement).getAttribute("href")),
  );
  for (const href of links.slice(0, 8)) {
    await page.goto(`${href}?tab=preview`);
    await page.waitForLoadState("networkidle");
  }

  expect(violations, violations.join("\n")).toEqual([]);

  // An empty list only means something if the detector works: inject an
  // inline script the policy must refuse, and require that it was seen.
  await page.evaluate(() => {
    const script = document.createElement("script");
    script.textContent = "window.__cspProbe = true";
    document.body.appendChild(script);
  });
  await expect.poll(() => violations.length).toBeGreaterThan(0);
  expect(await page.evaluate(() => (window as { __cspProbe?: boolean }).__cspProbe)).toBeUndefined();
});
