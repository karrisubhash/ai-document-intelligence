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

describe("DocumentsPage bulk delete", () => {
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

  it("deletes every matching document, in batches, once DELETE is typed", async () => {
    vi.spyOn(api, "documents").mockResolvedValue(page(ROWS, 150));
    const remove = vi
      .spyOn(api, "bulkDeleteDocuments")
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
    expect(screen.getByText("All 150 matching documents selected")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Delete all" }));
    const dialog = screen.getByRole("dialog", { name: "Delete all 150 matching documents?" });
    const confirm = within(dialog).getByRole("button", { name: "Delete all permanently" });
    expect(confirm).toBeDisabled();
    await userEvent.type(within(dialog).getByLabelText("Type DELETE to confirm"), "delete");
    await userEvent.click(confirm);

    expect(remove).toHaveBeenCalledTimes(2);
    expect(remove).toHaveBeenCalledWith({
      all: true,
      filters: {
        search: "",
        status: "COMPLETED",
        document_type: "",
        department: "",
        archived: "false",
        owner: "me",
      },
    });
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
});
