import { expect, test, type Page } from "@playwright/test";

import { PASSWORD } from "./fixtures";

/**
 * Deleting by selection on a live stack - documents, conversations and
 * collections - the way a user does it: tick, confirm, gone.
 *
 * Runs after platform.spec.ts (files run in name order, on one worker) and
 * as ravi, the faculty member in a department nothing else in the suite
 * uses. It creates what it deletes and leaves nothing of his behind.
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

/** Headers for an API call made with the signed-in page's session. */
async function writeHeaders(page: Page) {
  const token = (await page.context().cookies()).find((c) => c.name === "csrftoken")?.value;
  expect(token, "the signed-in session carries a CSRF cookie").toBeTruthy();
  return { "X-CSRFToken": token ?? "" };
}

/** Uploads through the API - the upload UI has its own test in platform.spec.ts. */
async function uploadQuietly(page: Page, title: string) {
  const response = await page.request.post("/api/v1/documents/", {
    headers: await writeHeaders(page),
    multipart: {
      title,
      document_type: "POLICY",
      file: {
        name: `${title.toLowerCase().replace(/\W+/g, "-")}.txt`,
        mimeType: "text/plain",
        buffer: Buffer.from(`${title}. Workshop safety rules, revised for the selection test.`),
      },
    },
  });
  expect(response.status(), await response.text()).toBe(202);
}

async function documentsMatching(page: Page, search: string) {
  const response = await page.request.get(
    `/api/v1/documents/?search=${encodeURIComponent(search)}&archived=all`,
  );
  expect(response.ok()).toBeTruthy();
  return (await response.json()) as { count: number; results: { status: string }[] };
}

/** Deleting while the worker is mid-pipeline is its own case; not this one. */
async function untilProcessed(page: Page, search: string, count: number) {
  await expect
    .poll(
      async () => {
        const listing = await documentsMatching(page, search);
        return listing.count === count && listing.results.every((row) => !IN_FLIGHT.includes(row.status));
      },
      { timeout: 180_000, intervals: [1_000, 2_000, 3_000] },
    )
    .toBe(true);
}

test("a user deletes the documents they ticked, then the rest of the page", async ({ page }) => {
  await signIn(page, "ravi");
  for (const n of [1, 2, 3]) await uploadQuietly(page, `Selection test ${n}`);
  await untilProcessed(page, "Selection test", 3);

  await page.goto("/documents?q=Selection%20test");
  await page.getByRole("checkbox", { name: "Select Selection test 1" }).check();
  await page.getByRole("checkbox", { name: "Select Selection test 2" }).check();
  await expect(page.getByText("2 selected")).toBeVisible();
  await page.screenshot({ path: `${SHOTS}/40-documents-selected.png`, fullPage: true });

  await page.getByRole("button", { name: "Delete selected" }).click();
  const dialog = page.getByRole("dialog", { name: "Delete 2 documents?" });
  await dialog.getByRole("button", { name: "Delete permanently" }).click();
  await expect(page.getByText("Deleted 2 documents.")).toBeVisible();
  await expect(page.getByRole("link", { name: "Selection test 1" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Selection test 3" })).toBeVisible();

  await page.getByRole("checkbox", { name: "Select all on this page" }).check();
  await page.getByRole("button", { name: "Delete selected" }).click();
  await page
    .getByRole("dialog", { name: "Delete 1 document?" })
    .getByRole("button", { name: "Delete permanently" })
    .click();
  await expect(page.getByText("No documents match these filters")).toBeVisible();

  // Gone on the server, not only from the screen.
  expect((await documentsMatching(page, "Selection test")).count).toBe(0);
});

test("'all matching' keeps to the snapshot it was counted in", async ({ page }) => {
  // Through the API on the live stack: the dialog that drives this needs
  // more than a page of documents, which the unit tests cover with mocks.
  await signIn(page, "ravi");
  for (const n of [1, 2]) await uploadQuietly(page, `Selection all ${n}`);
  await untilProcessed(page, "Selection all", 2);
  const headers = await writeHeaders(page);
  const filters = { search: "Selection all" };

  const preview = await page.request.post("/api/v1/documents/bulk-delete/", {
    headers,
    data: { all: true, filters, preview: true },
  });
  expect(preview.status()).toBe(200);
  const counted = await preview.json();
  expect(counted).toMatchObject({ deleted: [], remaining: 2, skipped: 0 });

  // A misspelt filter is refused, never read as "no filter".
  const typo = await page.request.post("/api/v1/documents/bulk-delete/", {
    headers,
    data: { all: true, filters: { q: "Selection all" }, as_of: counted.as_of },
  });
  expect(typo.status()).toBe(400);
  expect((await documentsMatching(page, "Selection all")).count).toBe(2);

  const deleted = await page.request.post("/api/v1/documents/bulk-delete/", {
    headers,
    data: { all: true, filters, as_of: counted.as_of },
  });
  expect(deleted.status()).toBe(200);
  expect(await deleted.json()).toMatchObject({ remaining: 0, failed: [] });
  expect((await documentsMatching(page, "Selection all")).count).toBe(0);
});

test("a user deletes all their conversations at once", async ({ page }) => {
  await signIn(page, "ravi");
  for (const question of ["Which workshops close on Sundays?", "Who signs a lab permit?"]) {
    const response = await page.request.post("/api/v1/chat/", {
      headers: await writeHeaders(page),
      data: { question },
    });
    expect(response.status(), await response.text()).toBe(200);
  }

  await page.goto("/ask");
  await page.getByRole("button", { name: "Select conversations" }).click();
  await page.getByRole("checkbox", { name: "Select all conversations" }).check();
  await page.screenshot({ path: `${SHOTS}/41-conversations-selected.png`, fullPage: true });
  await page.getByRole("button", { name: "Delete selected" }).click();
  const dialog = page.getByRole("dialog", { name: /^Delete all \d+ conversations\?$/ });
  await dialog.getByRole("button", { name: "Delete all" }).click();

  await expect(page.getByText(/^Deleted \d+ conversations\.$/)).toBeVisible();
  await expect(page.getByText("Your conversations will appear here.")).toBeVisible();
  const left = await page.request.get("/api/v1/chat/conversations/");
  expect((await left.json()).count).toBe(0);
});

test("a curator deletes several collections, and their documents stay", async ({ page }) => {
  await signIn(page, "ravi");
  await uploadQuietly(page, "Selection member");
  await untilProcessed(page, "Selection member", 1);
  const member = (await (await page.request.get("/api/v1/documents/?search=Selection%20member")).json())
    .results[0] as { id: number };

  for (const name of ["Selection shelf A", "Selection shelf B"]) {
    const created = await page.request.post("/api/v1/collections/", {
      headers: await writeHeaders(page),
      data: { name },
    });
    expect(created.status(), await created.text()).toBe(201);
    const { id } = (await created.json()) as { id: number };
    const added = await page.request.post(`/api/v1/collections/${id}/documents/`, {
      headers: await writeHeaders(page),
      data: { document_ids: [member.id] },
    });
    expect(added.ok()).toBeTruthy();
  }

  await page.goto("/collections");
  await page.getByRole("checkbox", { name: "Select all collections" }).check();
  await expect(page.getByText("2 selected")).toBeVisible();
  await page.screenshot({ path: `${SHOTS}/42-collections-selected.png`, fullPage: true });
  await page.getByRole("button", { name: "Delete all" }).click();
  await page
    .getByRole("dialog", { name: "Delete 2 collections?" })
    .getByRole("button", { name: "Delete permanently" })
    .click();

  await expect(page.getByText("Deleted 2 collections.")).toBeVisible();
  await expect(page.getByText("No collections yet")).toBeVisible();
  // The collections are gone; the document they held is not.
  expect((await documentsMatching(page, "Selection member")).count).toBe(1);

  // Leave nothing of ravi's behind.
  await page.goto("/documents?q=Selection%20member");
  await page.getByRole("checkbox", { name: "Select all on this page" }).check();
  await page.getByRole("button", { name: "Delete selected" }).click();
  await page
    .getByRole("dialog", { name: "Delete 1 document?" })
    .getByRole("button", { name: "Delete permanently" })
    .click();
  await expect(page.getByText("No documents match these filters")).toBeVisible();
});
