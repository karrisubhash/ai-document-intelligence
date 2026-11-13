import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import type { CreatedDocument, Me } from "../api/types";
import { AuthProvider } from "../auth/AuthContext";
import UploadDialog from "./UploadDialog";

const ME = {
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
} as Me;

beforeEach(() => {
  vi.spyOn(api, "me").mockResolvedValue(ME);
  vi.spyOn(api, "departments").mockResolvedValue([]);
});

afterEach(() => vi.restoreAllMocks());

describe("UploadDialog, a file already stored", () => {
  it("says so, names the readable copies, and opens the new one on request", async () => {
    const created = {
      id: 12,
      title: "Hostel rules",
      possible_duplicates: [
        {
          id: 5,
          title: "Hostel regulations",
          status: "COMPLETED",
          is_archived: false,
          is_current_version: true,
        },
      ],
    } as CreatedDocument;
    vi.spyOn(api, "upload").mockResolvedValue(created);
    const onUploaded = vi.fn();
    render(
      <MemoryRouter>
        <AuthProvider>
          <UploadDialog open onClose={vi.fn()} onUploaded={onUploaded} onBatchUploaded={vi.fn()} />
        </AuthProvider>
      </MemoryRouter>,
    );

    const dialog = await screen.findByRole("dialog", { name: "Upload documents" });
    const input = dialog.querySelector<HTMLInputElement>('input[type="file"]')!;
    await userEvent.upload(input, [new File(["same bytes"], "hostel.pdf")]);
    await userEvent.click(within(dialog).getByRole("button", { name: "Upload" }));

    expect(await within(dialog).findByText("This file was already here")).toBeInTheDocument();
    expect(within(dialog).getByRole("link", { name: "Hostel regulations" })).toHaveAttribute(
      "href",
      "/documents/5",
    );
    expect(onUploaded).not.toHaveBeenCalled();

    await userEvent.click(within(dialog).getByRole("button", { name: "Open the new document" }));
    expect(onUploaded).toHaveBeenCalledWith(created);
  });

  it("moves straight on when the file is new", async () => {
    const created = { id: 13, title: "New", possible_duplicates: [] } as unknown as CreatedDocument;
    vi.spyOn(api, "upload").mockResolvedValue(created);
    const onUploaded = vi.fn();
    render(
      <MemoryRouter>
        <AuthProvider>
          <UploadDialog open onClose={vi.fn()} onUploaded={onUploaded} onBatchUploaded={vi.fn()} />
        </AuthProvider>
      </MemoryRouter>,
    );
    const dialog = await screen.findByRole("dialog", { name: "Upload documents" });
    await userEvent.upload(dialog.querySelector<HTMLInputElement>('input[type="file"]')!, [
      new File(["fresh"], "new.pdf"),
    ]);
    await userEvent.click(within(dialog).getByRole("button", { name: "Upload" }));
    await vi.waitFor(() => expect(onUploaded).toHaveBeenCalledWith(created));
  });
});
