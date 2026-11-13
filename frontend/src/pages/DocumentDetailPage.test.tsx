import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, api } from "../api/client";
import type { DocumentDetail, DocumentStatus, Me, ProcessingJob } from "../api/types";
import App from "../App";
import { AuthProvider } from "../auth/AuthContext";
import { ToastProvider } from "../components/ui";

const ME: Me = {
  id: 1,
  username: "asha",
  email: "",
  first_name: "Asha",
  last_name: "Rao",
  is_superuser: false,
  role: "STUDENT",
  department: "Physics",
  capabilities: {
    administer_users: false,
    administer_departments: false,
    view_audit_log: false,
    view_operations: false,
    view_system_status: false,
    upload_to_any_department: false,
    create_collections: false,
  },
};

function document(status: DocumentStatus): DocumentDetail {
  return {
    id: 7,
    title: "Hostel circular",
    document_type: "GENERAL",
    department: "Physics",
    owner: 1,
    owner_username: "asha",
    status,
    current_version: {
      id: 3,
      version_number: 1,
      file_name: "circular.txt",
      file_size: 21,
      mime_type: "text/plain",
      uploaded_by: 1,
      created_at: "2026-10-05T10:00:00Z",
    },
    is_archived: false,
    archived_at: null,
    created_at: "2026-10-05T10:00:00Z",
    updated_at: "2026-10-05T10:00:00Z",
    can_manage: true,
  };
}

function job(status: ProcessingJob["status"]): ProcessingJob {
  return {
    id: 11,
    document_version: 3,
    status,
    current_stage: "INDEXING",
    attempt: 1,
    error_code: "",
    error_message: "",
    started_at: "2026-10-05T10:00:01Z",
    completed_at: status === "COMPLETED" ? "2026-10-05T10:00:03Z" : null,
    created_at: "2026-10-05T10:00:00Z",
    updated_at: "2026-10-05T10:00:03Z",
    events: [],
  } as ProcessingJob;
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.spyOn(api, "me").mockResolvedValue(ME);
  vi.spyOn(api, "departments").mockResolvedValue([]);
  vi.spyOn(api, "classification").mockResolvedValue([]);
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("DocumentDetailPage", () => {
  it("shows a document as ready even when it finished before the first status check", async () => {
    const detail = vi
      .spyOn(api, "document")
      .mockResolvedValueOnce(document("PROCESSING"))
      .mockResolvedValue(document("COMPLETED"));
    // The run is already over by the time the page first looks.
    vi.spyOn(api, "processing").mockResolvedValue([job("COMPLETED")]);

    render(
      <MemoryRouter initialEntries={["/documents/7"]}>
        <AuthProvider>
          <ToastProvider>
            <App />
          </ToastProvider>
        </AuthProvider>
      </MemoryRouter>,
    );
    expect(await screen.findByRole("heading", { name: "Hostel circular" })).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_500);
    });

    expect(detail).toHaveBeenCalledTimes(2);
    expect(await screen.findByText("Ready", { selector: ".page-header .badge, .page-header .badge *" })).toBeInTheDocument();
  });

  it("names itself in the top bar, and the name goes when the page does", async () => {
    vi.spyOn(api, "document").mockResolvedValue(document("COMPLETED"));
    vi.spyOn(api, "processing").mockResolvedValue([job("COMPLETED")]);
    vi.spyOn(api, "documents").mockResolvedValue({ count: 0, next: null, previous: null, results: [] });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    render(
      <MemoryRouter initialEntries={["/documents/7"]}>
        <AuthProvider>
          <ToastProvider>
            <App />
          </ToastProvider>
        </AuthProvider>
      </MemoryRouter>,
    );

    const trail = await screen.findByRole("navigation", { name: "Breadcrumb" });
    expect(within(trail).getByText("Hostel circular")).toHaveAttribute("aria-current", "page");
    const parent = within(trail).getByRole("link", { name: "Documents" });
    expect(parent).toHaveAttribute("href", "/documents");

    await user.click(parent);
    expect(await screen.findByRole("heading", { name: "Documents" })).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByRole("navigation", { name: "Breadcrumb" })).not.toBeInTheDocument(),
    );
  });
});

describe("Edit details", () => {
  /** The same document once the type detector has made it a circular. */
  function circular(status: DocumentStatus = "COMPLETED"): DocumentDetail {
    return {
      ...document(status),
      document_type: "CIRCULAR",
      document_type_source: "auto",
      type_confidence: 0.92,
    };
  }

  async function openEditDialog(user: ReturnType<typeof userEvent.setup>) {
    render(
      <MemoryRouter initialEntries={["/documents/7"]}>
        <AuthProvider>
          <ToastProvider>
            <App />
          </ToastProvider>
        </AuthProvider>
      </MemoryRouter>,
    );
    await user.click(await screen.findByRole("button", { name: "More actions" }));
    await user.click(screen.getByRole("menuitem", { name: "Edit details" }));
    return screen.findByRole("dialog", { name: "Edit details" });
  }

  it("saves a title without the type, though the detector typed the document while the form was open", async () => {
    vi.spyOn(api, "document")
      .mockResolvedValueOnce({ ...document("PROCESSING"), document_type_source: "default" })
      .mockResolvedValue(circular());
    // The run settles while the form is open, and the page reloads the document.
    vi.spyOn(api, "processing").mockResolvedValue([job("COMPLETED")]);
    const update = vi
      .spyOn(api, "updateDocument")
      .mockImplementation(async (_id, patch) => ({ ...circular(), title: patch.title ?? "" }));
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });

    const dialog = await openEditDialog(user);
    const type = within(dialog).getByRole("combobox", { name: "Type" });
    expect(type).toHaveValue("GENERAL");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_500);
    });
    // Untouched, the list shows the type the document has now.
    await waitFor(() => expect(type).toHaveValue("CIRCULAR"));

    const title = within(dialog).getByRole("textbox", { name: "Title" });
    await user.clear(title);
    await user.type(title, "Bus pass circular");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    await waitFor(() => expect(update).toHaveBeenCalledTimes(1));
    // No type at all: sending back the one the form opened with recorded
    // that stale General as a person's choice, which the detector never
    // overrides.
    expect(update.mock.calls[0][1]).toEqual({ title: "Bus pass circular" });
  });

  it("sends a type the person picked with the type it was picked over", async () => {
    vi.spyOn(api, "document").mockResolvedValue(document("COMPLETED"));
    vi.spyOn(api, "processing").mockResolvedValue([job("COMPLETED")]);
    const update = vi.spyOn(api, "updateDocument").mockResolvedValue({
      ...document("COMPLETED"),
      document_type: "POLICY",
      document_type_source: "user",
    });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });

    const dialog = await openEditDialog(user);
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Type" }), "POLICY");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    await waitFor(() => expect(update).toHaveBeenCalledTimes(1));
    expect(update.mock.calls[0][1]).toEqual({
      title: "Hostel circular",
      document_type: "POLICY",
      expected_document_type: "GENERAL",
    });
  });

  it("after a refusal because the type moved, shows why and the type it has now", async () => {
    vi.spyOn(api, "document")
      .mockResolvedValueOnce(document("COMPLETED"))
      .mockResolvedValue(circular());
    vi.spyOn(api, "processing").mockResolvedValue([job("COMPLETED")]);
    const message =
      "The type was changed to Circular while this form was open, so nothing was saved. " +
      "Check the type and save again.";
    const update = vi
      .spyOn(api, "updateDocument")
      .mockRejectedValueOnce(new ApiError(409, "Conflict", { document_type: [message] }))
      .mockResolvedValueOnce({ ...circular(), document_type: "POLICY", document_type_source: "user" });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });

    const dialog = await openEditDialog(user);
    const type = within(dialog).getByRole("combobox", { name: "Type" });
    await user.selectOptions(type, "POLICY");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    expect(await within(dialog).findByText(message)).toBeInTheDocument();
    await waitFor(() => expect(type).toHaveValue("CIRCULAR"));

    // Chosen again, knowing what it is now: sent against the current type.
    await user.selectOptions(type, "POLICY");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(update).toHaveBeenCalledTimes(2));
    expect(update.mock.calls[1][1]).toEqual({
      title: "Hostel circular",
      document_type: "POLICY",
      expected_document_type: "CIRCULAR",
    });
  });
});
