import { act, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
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
});
