import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import type { DocumentSummary, Me } from "../api/types";
import App from "../App";
import { AuthProvider } from "../auth/AuthContext";
import { ToastProvider } from "../components/ui";

const FACULTY: Me = {
  id: 1,
  username: "prof",
  email: "",
  first_name: "Ravi",
  last_name: "K",
  is_superuser: false,
  role: "FACULTY",
  department: "Computer Science",
  capabilities: {
    administer_users: false,
    administer_departments: false,
    view_audit_log: false,
    view_operations: false,
    view_system_status: false,
    upload_to_any_department: false,
    create_collections: true,
  },
};

const row = (id: number, title: string, can_manage = true): DocumentSummary => ({
  id,
  title,
  department: "Computer Science",
  document_type: "POLICY",
  owner: can_manage ? 1 : 2,
  owner_username: can_manage ? "prof" : "other",
  status: "COMPLETED",
  current_version: null,
  is_archived: false,
  archived_at: null,
  can_manage,
  created_at: "2026-10-01T10:00:00Z",
  updated_at: "2026-10-01T10:00:00Z",
});

const page = (results: DocumentSummary[], count = results.length) => ({
  count,
  next: null,
  previous: null,
  results,
});

// The first query of each test waits for the app shell and the listing to
// load, which takes longer than the 1 s default on a busy machine.
const LOADED = { timeout: 5000 };

const ROWS = [row(1, "Fee schedule"), row(2, "Leave policy"), row(3, "Colleague's notes", false)];

function renderAt(path: string) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <AuthProvider>
        <ToastProvider>
          <App />
        </ToastProvider>
      </AuthProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.spyOn(api, "me").mockResolvedValue(FACULTY);
  vi.spyOn(api, "departments").mockResolvedValue([]);
});

afterEach(() => vi.restoreAllMocks());

describe("DocumentsPage bulk delete", { timeout: 15000 }, () => {
  it("deletes the selected documents after confirmation", async () => {
    const list = vi.spyOn(api, "documents").mockResolvedValue(page(ROWS));
    const remove = vi
      .spyOn(api, "bulkDeleteDocuments")
      .mockResolvedValue({ deleted: [1, 2], forbidden: [], not_found: [] });
    renderAt("/documents");

    await userEvent.click(await screen.findByRole("checkbox", { name: "Select Fee schedule" }, LOADED));
    await userEvent.click(screen.getByRole("checkbox", { name: "Select Leave policy" }));
    expect(screen.getByText("2 selected")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Delete selected" }));
    const dialog = screen.getByRole("dialog", { name: "Delete 2 documents?" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete permanently" }));

    expect(remove).toHaveBeenCalledWith({ ids: [1, 2] });
    expect(await screen.findByText("Deleted 2 documents.")).toBeInTheDocument();
    expect(list).toHaveBeenCalledTimes(2);
    expect(screen.queryByText("2 selected")).not.toBeInTheDocument();
  });

  it("shows the selection bar only once something is ticked, below the table", async () => {
    vi.spyOn(api, "documents").mockResolvedValue(page(ROWS));
    renderAt("/documents");

    const box = await screen.findByRole("checkbox", { name: "Select Fee schedule" }, LOADED);
    expect(screen.queryByRole("region", { name: "Selected documents" })).not.toBeInTheDocument();

    await userEvent.click(box);
    const bar = screen.getByRole("region", { name: "Selected documents" });
    // After the table in the page, so appearing never moves the rows.
    const table = screen.getByRole("table");
    expect(table.compareDocumentPosition(bar) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByText("1 document selected")).toHaveClass("visually-hidden");
  });

  it("offers only the documents the caller may delete", async () => {
    vi.spyOn(api, "documents").mockResolvedValue(page(ROWS));
    renderAt("/documents");

    expect(await screen.findByRole("checkbox", { name: "Select Colleague's notes" }, LOADED)).toBeDisabled();
    await userEvent.click(screen.getByRole("checkbox", { name: "Select all on this page" }));

    expect(screen.getByText("2 selected")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Select Colleague's notes" })).not.toBeChecked();
  });

  it("shows no selection column when nothing on the page can be deleted", async () => {
    vi.spyOn(api, "documents").mockResolvedValue(page([row(3, "Colleague's notes", false)]));
    renderAt("/documents");

    expect(await screen.findByText("Colleague's notes", {}, LOADED)).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: /^Select/ })).not.toBeInTheDocument();
  });

  it("deletes every matching document it may, within the counted snapshot", async () => {
    vi.spyOn(api, "documents").mockResolvedValue(page(ROWS, 150));
    const remove = vi
      .spyOn(api, "bulkDeleteDocuments")
      // The preview: what "all" means is counted by the server.
      .mockResolvedValueOnce({
        deleted: [],
        forbidden: [],
        not_found: [],
        remaining: 140,
        skipped: 10,
        as_of: "2026-10-20T09:00:00Z",
      })
      .mockResolvedValueOnce({
        deleted: Array.from({ length: 100 }, (_, n) => n + 10),
        forbidden: [],
        not_found: [],
        remaining: 40,
        skipped: 10,
      })
      .mockResolvedValueOnce({
        deleted: Array.from({ length: 40 }, (_, n) => n + 200),
        forbidden: [],
        not_found: [],
        remaining: 0,
        skipped: 10,
      });
    renderAt("/documents?status=COMPLETED&mine=1");

    await userEvent.click(await screen.findByRole("checkbox", { name: "Select Fee schedule" }, LOADED));
    await userEvent.click(screen.getByRole("button", { name: "Select all 150 matching" }));
    expect(screen.getByText("All matching documents selected (150)")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Delete all…" }));
    const filters = {
      search: "",
      status: "COMPLETED",
      document_type: "",
      department: "",
      archived: "false",
      owner: "me",
    };
    expect(remove).toHaveBeenCalledWith({ all: true, filters, preview: true });
    // The title counts what will be deleted, not the listing's 150.
    const dialog = await screen.findByRole("dialog", { name: "Delete 140 matching documents?" });
    expect(
      within(dialog).getByText("10 matching documents you may not delete are left in place."),
    ).toBeInTheDocument();
    const confirm = within(dialog).getByRole("button", { name: "Delete all permanently" });
    expect(confirm).toBeDisabled();
    await userEvent.type(within(dialog).getByLabelText("Type DELETE to confirm"), "delete");
    await userEvent.click(confirm);

    expect(remove).toHaveBeenCalledTimes(3);
    // Every round keeps to the preview's snapshot.
    expect(remove).toHaveBeenLastCalledWith({ all: true, filters, as_of: "2026-10-20T09:00:00Z" });
    expect(
      await screen.findByText("Deleted 140 documents. 10 you may not delete were left in place."),
    ).toBeInTheDocument();
  });

  it("keeps the dialog open with the error when deleting fails", async () => {
    vi.spyOn(api, "documents").mockResolvedValue(page(ROWS));
    vi.spyOn(api, "bulkDeleteDocuments").mockRejectedValue(new Error("Server unavailable"));
    renderAt("/documents");

    await userEvent.click(await screen.findByRole("checkbox", { name: "Select Fee schedule" }, LOADED));
    await userEvent.click(screen.getByRole("button", { name: "Delete selected" }));
    const dialog = screen.getByRole("dialog", { name: "Delete 1 document?" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete permanently" }));

    expect(await within(dialog).findByText("Deleting stopped")).toBeInTheDocument();
    expect(screen.getByText("1 selected")).toBeInTheDocument();
  });

  it("refreshes the list after a failure part-way, since earlier batches were deleted", async () => {
    const many = Array.from({ length: 120 }, (_, n) => row(n + 1, `Doc ${n + 1}`));
    const list = vi
      .spyOn(api, "documents")
      .mockResolvedValueOnce(page(many))
      .mockResolvedValue(page(many.slice(100)));
    vi.spyOn(api, "bulkDeleteDocuments")
      .mockResolvedValueOnce({
        deleted: many.slice(0, 100).map((doc) => doc.id),
        forbidden: [],
        not_found: [],
      })
      .mockRejectedValueOnce(new Error("Gateway timeout"));
    renderAt("/documents");

    await userEvent.click(await screen.findByRole("checkbox", { name: "Select all on this page" }, LOADED));
    await userEvent.click(screen.getByRole("button", { name: "Delete selected" }));
    const dialog = screen.getByRole("dialog", { name: "Delete 120 documents?" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete permanently" }));

    expect(await within(dialog).findByText("Deleting stopped")).toBeInTheDocument();
    expect(
      within(dialog).getByText(/100 documents were deleted before it stopped/),
    ).toBeInTheDocument();
    expect(list).toHaveBeenCalledTimes(2);
    // The 100 deleted are no longer selected; the 20 not reached still are.
    expect(await screen.findByText("20 selected")).toBeInTheDocument();
  });

  it("keeps the rest of the page when one row is unticked from 'all matching'", async () => {
    vi.spyOn(api, "documents").mockResolvedValue(page(ROWS, 150));
    renderAt("/documents");

    await userEvent.click(await screen.findByRole("checkbox", { name: "Select Fee schedule" }, LOADED));
    await userEvent.click(screen.getByRole("button", { name: "Select all 150 matching" }));
    // The one ticked before "all" is the one unticked: the rest of the
    // page, shown ticked under "all", must stay ticked.
    await userEvent.click(screen.getByRole("checkbox", { name: "Select Fee schedule" }));

    expect(screen.getByText("1 selected")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Select Leave policy" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Select Fee schedule" })).not.toBeChecked();
  });
});
