import { expect, test, type Page } from "@playwright/test";

import { PASSWORD, makePdf, makeZip } from "./fixtures";

/**
 * Several documents at once on a live stack: files chosen together and a
 * zip of more, in one go through the upload dialog. Each must come out as
 * its own document, processed exactly as if it had been uploaded alone.
 *
 * Runs last (files run in name order) as ravi, and deletes what it uploads.
 */
test.describe.configure({ mode: "serial" });

test.skip(!PASSWORD, "E2E_PASSWORD is not set; see docs/development/testing.md");

const SHOTS = "e2e/.results/screens";
const IN_FLIGHT = ["UPLOADED", "QUEUED", "PROCESSING"];

async function signIn(page: Page, username: string) {
  await page.goto("/");
  await page.getByLabel("Username").fill(username);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: /^Welcome,/ })).toBeVisible();
}

type Row = { id: number; title: string; status: string; current_version: { file_name: string; mime_type: string } | null };

async function documentsMatching(page: Page, search: string) {
  const response = await page.request.get(
    `/api/v1/documents/?search=${encodeURIComponent(search)}&archived=all`,
  );
  expect(response.ok()).toBeTruthy();
  return (await response.json()) as { count: number; results: Row[] };
}

test("a user uploads several files and a zip of more in one go", async ({ page }) => {
  // The zip is opened in the page. Behind the production policy (the
  // harness runs this spec there too), nothing it does may be refused.
  const violations: string[] = [];
  page.on("console", (message) => {
    if (/Content Security Policy|Refused to/i.test(message.text())) violations.push(message.text());
  });
  await page.addInitScript(() => {
    document.addEventListener("securitypolicyviolation", (event) => {
      console.error(`Refused to load (CSP ${event.violatedDirective}): ${event.blockedURI || "inline"}`);
    });
  });

  await signIn(page, "ravi");
  await page.getByRole("link", { name: "Documents" }).first().click();
  await page.getByRole("button", { name: "Upload document" }).first().click();
  const dialog = page.getByRole("dialog", { name: "Upload documents" });

  await dialog.locator('input[type="file"]').setInputFiles([
    {
      name: "batch-canteen-hours.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("Batch upload notice. The canteen serves lunch from noon until 2 pm."),
    },
    {
      name: "batch-sports-day.pdf",
      mimeType: "application/pdf",
      buffer: makePdf([["Batch upload notice", "Sports day is held on the first Friday of February."]]),
    },
    {
      name: "batch-circulars.zip",
      mimeType: "application/zip",
      buffer: makeZip([
        { name: "circulars/batch-library-hours.txt", data: "Batch upload notice. The library opens at 8 am." },
        {
          name: "circulars/batch-bus-routes.txt",
          data: "Batch upload notice. Route 4 now stops at the north gate.",
          stored: true,
        },
        // A macro-enabled workbook: .xlsx and the old .xls are both read now
        // (the .xls through LibreOffice), this is not.
        { name: "circulars/timetable.xlsm", data: "not a format the platform reads" },
        { name: "__MACOSX/circulars/._batch-library-hours.txt", data: "Finder metadata" },
      ]),
    },
  ]);

  await expect(dialog.getByText("4 files to upload · 1 skipped")).toBeVisible();
  await expect(dialog.getByText("in batch-circulars.zip › circulars").first()).toBeVisible();
  const skipped = dialog.locator(".upload-row", { hasText: "timetable.xlsm" });
  await expect(skipped.getByText("Skipped")).toBeVisible();
  await expect(dialog.getByText("._batch-library-hours.txt")).toHaveCount(0);
  await page.screenshot({ path: `${SHOTS}/40-batch-chosen.png`, fullPage: true });

  await dialog.getByRole("button", { name: "Upload 4 documents" }).click();
  await expect(dialog.getByText("4 documents uploaded")).toBeVisible({ timeout: 90_000 });
  await page.screenshot({ path: `${SHOTS}/41-batch-uploaded.png`, fullPage: true });
  await dialog.getByRole("button", { name: "Done" }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByRole("link", { name: "Batch bus routes" }).first()).toBeVisible();

  // Four documents, each through the whole pipeline on its own.
  await expect
    .poll(
      async () => {
        const listing = await documentsMatching(page, "Batch");
        return listing.count === 4 && listing.results.every((row) => !IN_FLIGHT.includes(row.status));
      },
      { timeout: 180_000, intervals: [1_000, 2_000, 3_000] },
    )
    .toBe(true);
  const { results } = await documentsMatching(page, "Batch");
  expect(results.map((row) => [row.title, row.status]).sort()).toEqual([
    ["Batch bus routes", "COMPLETED"],
    ["Batch canteen hours", "COMPLETED"],
    ["Batch library hours", "COMPLETED"],
    ["Batch sports day", "COMPLETED"],
  ]);
  const routes = results.find((row) => row.title === "Batch bus routes");
  expect(routes?.current_version).toMatchObject({ file_name: "batch-bus-routes.txt", mime_type: "text/plain" });

  // A file from inside the zip reads like any other.
  await page.getByRole("link", { name: "Batch bus routes" }).first().click();
  await page.getByRole("tab", { name: "Text" }).click();
  await expect(page.locator(".page-text")).toContainText("Route 4 now stops at the north gate");

  const token = (await page.context().cookies()).find((c) => c.name === "csrftoken")?.value ?? "";
  const deleted = await page.request.post("/api/v1/documents/bulk-delete/", {
    headers: { "X-CSRFToken": token },
    data: { ids: results.map((row) => row.id) },
  });
  expect(deleted.status()).toBe(200);
  expect((await documentsMatching(page, "Batch")).count).toBe(0);
  expect(violations).toEqual([]);
});
