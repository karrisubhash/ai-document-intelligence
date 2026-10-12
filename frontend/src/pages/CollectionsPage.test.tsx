import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import type { Collection, DocumentSummary, Me } from "../api/types";
import App from "../App";
import { AuthProvider } from "../auth/AuthContext";
import { ToastProvider } from "../components/ui";

const capabilities = {
  administer_users: false,
  administer_departments: false,
  view_audit_log: false,
  view_operations: false,
  view_system_status: false,
  upload_to_any_department: false,
  create_collections: true,
};

const FACULTY: Me = {
  id: 1,
  username: "prof",
  email: "",
  first_name: "Ravi",
  last_name: "K",
  is_superuser: false,
  role: "FACULTY",
  department: "Computer Science",
  capabilities,
};

const RULES: Collection = {
  id: 7,
  name: "Exam rules",
  description: "",
  department: "Computer Science",
  documents: [{ id: 3, title: "Attendance policy" }],
  document_count: 1,
  created_by: "prof",
  can_manage: true,
  created_at: "2026-10-01T10:00:00Z",
  updated_at: "2026-10-01T10:00:00Z",
};

const page = <T,>(results: T[]) => ({ count: results.length, next: null, previous: null, results });

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
  vi.spyOn(api, "collections").mockResolvedValue(page([RULES]));
  vi.spyOn(api, "documents").mockResolvedValue(page([]));
});

afterEach(() => vi.restoreAllMocks());

describe("CollectionsPage", () => {
  it("lists collections with their documents and creates a new one", async () => {
    vi.spyOn(api, "me").mockResolvedValue(FACULTY);
    const create = vi.spyOn(api, "createCollection").mockResolvedValue({ ...RULES, id: 8, name: "Fees" });
    renderAt("/collections");

    expect(await screen.findByText("Exam rules")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Attendance policy" })).toHaveAttribute("href", "/documents/3");

    await userEvent.type(screen.getByLabelText(/^Name/), "Fees");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));
    expect(create).toHaveBeenCalledWith({ name: "Fees", description: "" });
  });

  it("hides creation and management from a reader", async () => {
    vi.spyOn(api, "me").mockResolvedValue({
      ...FACULTY,
      role: "STUDENT",
      capabilities: { ...capabilities, create_collections: false },
    });
    vi.spyOn(api, "collections").mockResolvedValue(page([{ ...RULES, can_manage: false }]));
    renderAt("/collections");

    expect(await screen.findByText("Exam rules")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Remove" })).not.toBeInTheDocument();
  });

  it("lets a system administrator say which department a collection is for", async () => {
    vi.spyOn(api, "me").mockResolvedValue({
      ...FACULTY,
      role: "SYSTEM_ADMIN",
      department: "",
      capabilities: { ...capabilities, upload_to_any_department: true },
    });
    vi.spyOn(api, "departments").mockResolvedValue([{ id: 2, name: "Mechanical", code: "ME" }]);
    const create = vi
      .spyOn(api, "createCollection")
      .mockResolvedValue({ ...RULES, id: 9, name: "Workshops", department: "Mechanical" });
    renderAt("/collections");

    await userEvent.type(await screen.findByLabelText(/^Name/), "Workshops");
    await userEvent.selectOptions(
      screen.getByLabelText(/^Department/),
      await screen.findByRole("option", { name: "Mechanical" }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Create" }));
    expect(create).toHaveBeenCalledWith({
      name: "Workshops",
      description: "",
      department: "Mechanical",
    });
  });

  it("offers only documents from the collection's own department", async () => {
    vi.spyOn(api, "me").mockResolvedValue(FACULTY);
    const document = (id: number, title: string, department: string): DocumentSummary => ({
      id,
      title,
      department,
      document_type: "POLICY",
      owner: 1,
      owner_username: "prof",
      status: "COMPLETED",
      current_version: null,
      is_archived: false,
      archived_at: null,
      created_at: "2026-10-01T10:00:00Z",
      updated_at: "2026-10-01T10:00:00Z",
    });
    vi.spyOn(api, "documents").mockResolvedValue(
      page([document(4, "Fee schedule", "Computer Science"), document(5, "Workshop rules", "Mechanical")]),
    );
    renderAt("/collections");

    expect(await screen.findByRole("option", { name: "Fee schedule" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Workshop rules" })).not.toBeInTheDocument();
  });
});
