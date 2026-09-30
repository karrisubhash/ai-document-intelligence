import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test, type Page } from "@playwright/test";

import { PASSWORD, corpusFile, makePdf } from "./fixtures";

/**
 * The whole product path, in order, on a live stack:
 *
 *   sign in -> upload -> scan/validate -> async processing -> OCR/parsing
 *   -> classification -> extraction -> indexing -> search -> open the
 *   source -> ask -> retrieve -> rerank -> answer -> cite -> abstain
 *   -> tenant boundary -> administration -> audit
 *
 * Serial on purpose: later steps use the documents earlier ones uploaded,
 * the way a real user's session does.
 */
test.describe.configure({ mode: "serial" });

test.skip(!PASSWORD, "E2E_PASSWORD is not set; see docs/development/testing.md");

const SHOTS = "e2e/.results/screens";

async function signIn(page: Page, username: string) {
  await page.goto("/");
  await page.getByLabel("Username").fill(username);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: /^Welcome,/ })).toBeVisible();
}

async function signOut(page: Page) {
  await page.locator(".user-button").click();
  await page.getByRole("menuitem", { name: "Sign out" }).click();
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
}

async function upload(page: Page, file: { name: string; mimeType: string; buffer: Buffer }, title: string) {
  await page.getByRole("button", { name: "Upload document" }).first().click();
  const dialog = page.getByRole("dialog", { name: "Upload a document" });
  await dialog.locator('input[type="file"]').setInputFiles(file);
  await dialog.getByLabel(/^Title/).fill(title);
  await dialog.getByRole("button", { name: "Upload", exact: true }).click();
  await expect(page.getByRole("heading", { name: title })).toBeVisible();
  // Processing is asynchronous; the page polls until the worker finishes.
  await expect(page.locator(".page-header .badge", { hasText: "Ready" })).toBeVisible({
    timeout: 180_000,
  });
}

let regulationsUrl = "";

test("a student uploads documents and they are processed end to end", async ({ page }) => {
  await signIn(page, "asha");
  await page.screenshot({ path: `${SHOTS}/01-dashboard.png`, fullPage: true });

  await upload(page, corpusFile("academic-regulations"), "Academic Regulations R20");
  regulationsUrl = page.url();

  await page.getByRole("tab", { name: "Processing" }).click();
  for (const stage of ["Security scan", "Text extraction", "Classification", "Indexing"]) {
    await expect(page.locator(".timeline-stage", { hasText: stage }).first()).toBeVisible();
  }
  await expect(page.locator(".timeline .badge", { hasText: "Failed" })).toHaveCount(0);
  await page.screenshot({ path: `${SHOTS}/02-processing.png`, fullPage: true });

  await page.getByRole("tab", { name: "Text" }).click();
  await expect(page.locator(".page-text")).toContainText("minimum of 75 percent attendance");

  await page.getByRole("link", { name: "Documents" }).first().click();
  await upload(
    page,
    {
      name: "hostel-rules.pdf",
      mimeType: "application/pdf",
      buffer: makePdf([
        [
          "HOSTEL REGULATIONS",
          "Clause 3.1 Curfew",
          "The hostel curfew is 10 pm on weekdays and 11 pm on Saturdays.",
          "Residents returning later must have written permission from the warden.",
        ],
        [
          "Clause 3.6 Visitors",
          "Visitors are permitted in the common room only, between 4 pm and 7 pm.",
        ],
      ]),
    },
    "Hostel Regulations",
  );
  await page.getByRole("tab", { name: "Preview" }).click();
  await expect(page.locator("iframe.preview-frame")).toBeVisible();
  await page.getByRole("tab", { name: "Text" }).click();
  await page.getByRole("combobox").first().selectOption("2");
  await expect(page.locator(".page-text")).toContainText("common room");
  await page.screenshot({ path: `${SHOTS}/03-document-text.png`, fullPage: true });
});

test("a scanned image is read by OCR and becomes searchable", async ({ page }) => {
  await signIn(page, "asha");
  await page.getByRole("link", { name: "Documents" }).first().click();
  // No text layer at all: the only route from pixels to the index is OCR.
  await upload(
    page,
    {
      name: "scanned-circular.png",
      mimeType: "image/png",
      buffer: readFileSync(join(dirname(fileURLToPath(import.meta.url)), "fixtures", "scanned-circular.png")),
    },
    "Workshop Closure Circular",
  );

  await page.getByRole("tab", { name: "Text" }).click();
  await expect(page.locator(".content-toolbar .badge", { hasText: "Read by OCR" })).toBeVisible();
  await expect(page.locator(".page-text")).toContainText("second Saturday");

  await page.getByRole("link", { name: "Search" }).first().click();
  await page.getByLabel("Search documents").fill("workshop closed for maintenance");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(page.locator(".result").first()).toContainText("Workshop Closure Circular", {
    timeout: 90_000,
  });
});

test("search finds a passage and opens it at its source", async ({ page }) => {
  await signIn(page, "asha");
  await page.getByRole("link", { name: "Search" }).first().click();
  await page.getByLabel("Search documents").fill("minimum attendance for the examination");
  await page.getByRole("button", { name: "Search", exact: true }).click();

  const first = page.locator(".result").first();
  // Generous: under the dev server the first query in a process loads the
  // embedding model (gunicorn workers warm it at start instead).
  await expect(first).toContainText("Academic Regulations R20", { timeout: 90_000 });
  await expect(first.locator("mark").first()).toBeVisible();
  await page.screenshot({ path: `${SHOTS}/04-search.png`, fullPage: true });

  await first.locator(".result-title").click();
  await expect(page.locator("mark.evidence")).toBeVisible();
  await expect(page.locator("mark.evidence")).toContainText("attendance");
});

test("a question is answered with a citation that opens its evidence", async ({ page }) => {
  await signIn(page, "asha");
  await page.getByRole("link", { name: "Ask" }).first().click();
  await page
    .getByLabel("Your question")
    .fill("What is the minimum attendance required to sit the end-semester examination?");
  await page.getByRole("button", { name: "Ask", exact: true }).click();

  const answer = page.locator(".bubble-assistant").last();
  await expect(answer.locator(".answer")).toBeVisible({ timeout: 120_000 });
  await expect(answer).toContainText("75 percent");
  await page.screenshot({ path: `${SHOTS}/05-answer.png`, fullPage: true });

  await answer.locator("button.citation").first().click();
  const source = page.getByRole("dialog", { name: /Source \[1\]/ });
  await expect(source.locator("mark.evidence").first()).toContainText("75 percent");
  await page.screenshot({ path: `${SHOTS}/06-source.png`, fullPage: true });
  await source.getByRole("button", { name: "Close" }).click();

  // A follow-up in the same conversation keeps its context.
  await page.getByLabel("Your question").fill("What about condonation?");
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  const followUp = page.locator(".bubble-assistant").last();
  await expect(followUp.locator(".answer")).toBeVisible({ timeout: 120_000 });
  // Asserted on the answer PROSE. The bubble also shows "read as a
  // follow-up: ...condonation...", and an assertion on the whole bubble
  // passed while the answer itself repeated the attendance clause.
  const prose = followUp.locator(".prose");
  await expect(prose).toContainText("65");
  await expect(prose).toContainText(/condonation/i);
  await expect(prose).not.toContainText("in each course");

  // And a question the documents cannot answer is declined, not guessed.
  await page.getByLabel("Your question").fill("What is the parking fee for visiting parents?");
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  const declined = page.locator(".bubble-assistant").last();
  await expect(declined.getByText("Not enough evidence to answer")).toBeVisible({
    timeout: 120_000,
  });
  await page.screenshot({ path: `${SHOTS}/07-abstention.png`, fullPage: true });
});

test("another department cannot see these documents at all", async ({ page }) => {
  await signIn(page, "ravi");
  await page.getByRole("link", { name: "Documents" }).first().click();
  await expect(page.getByText("No documents yet")).toBeVisible();

  await page.goto(regulationsUrl);
  await expect(page.getByText("This document could not be opened")).toBeVisible();
  await expect(page.getByText(/does not exist, or you do not have access/)).toBeVisible();

  await page.getByRole("link", { name: "Ask" }).first().click();
  await page.getByLabel("Your question").fill("What is the minimum attendance required?");
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  await expect(
    page.locator(".bubble-assistant").last().getByText("Not enough evidence to answer"),
  ).toBeVisible({ timeout: 120_000 });
  await signOut(page);
});

test("a department admin manages their people and sees the denied attempt", async ({ page }) => {
  await signIn(page, "cs_admin");
  await page.getByRole("link", { name: "Users" }).click();
  const table = page.locator("table");
  await expect(table).toContainText("asha");
  await expect(table).not.toContainText("ravi");

  const username = `student_${Date.now().toString(36)}`;
  await page.getByRole("button", { name: "New account" }).click();
  const dialog = page.getByRole("dialog", { name: "New account" });
  await dialog.getByLabel(/^Username/).fill(username);
  await dialog.getByLabel(/^Initial password/).fill(`${PASSWORD}-new`);
  await dialog.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByText(`Account ${username} created.`)).toBeVisible();
  await page.screenshot({ path: `${SHOTS}/08-users.png`, fullPage: true });

  await page.getByRole("link", { name: "Audit log" }).click();
  await page.getByLabel("Result").selectOption("DENIED");
  await expect(page.locator("tbody tr", { hasText: "ravi" }).first()).toBeVisible();
  await page.screenshot({ path: `${SHOTS}/09-audit.png`, fullPage: true });
});

test("a system admin sees every dependency healthy and the AI configuration", async ({ page }) => {
  await signIn(page, "admin");
  await page.getByRole("link", { name: "System status" }).click();
  for (const component of ["Database", "Cache", "Broker", "Storage"]) {
    await expect(page.locator(".health", { hasText: component })).toContainText("Healthy");
  }
  // Whatever this deployment is configured with, the screen must report
  // exactly what the API says is running - not a hard-coded expectation.
  const status = await page.request.get("/api/admin/system/");
  const embedding = (await status.json()).ai.embedding.provider as string;
  const row = page.locator(".dl-row", { hasText: "Embeddings" });
  await expect(row).toContainText(embedding || "None");
  await page.screenshot({ path: `${SHOTS}/10-system.png`, fullPage: true });

  await page.getByRole("link", { name: "Processing" }).click();
  await expect(page.getByRole("heading", { name: "Processing" })).toBeVisible();
});
