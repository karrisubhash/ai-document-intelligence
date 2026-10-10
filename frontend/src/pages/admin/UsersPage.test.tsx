import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, api } from "../../api/client";
import type { AdminUser, Me } from "../../api/types";
import App from "../../App";
import { AuthProvider } from "../../auth/AuthContext";
import { ToastProvider } from "../../components/ui";

const ADMIN: Me = {
  id: 1,
  username: "cs_admin",
  email: "",
  first_name: "Meera",
  last_name: "Iyer",
  is_superuser: false,
  role: "DEPARTMENT_ADMIN",
  department: "Computer Science",
  capabilities: {
    administer_users: true,
    administer_departments: false,
    view_audit_log: true,
    view_operations: true,
    view_system_status: false,
    upload_to_any_department: false,
  },
};

const STUDENT: AdminUser = {
  id: 2,
  username: "asha",
  email: "",
  first_name: "Asha",
  last_name: "Rao",
  is_active: true,
  is_superuser: false,
  role: "STUDENT",
  department: "Computer Science",
  last_login: null,
  date_joined: "2026-10-01T10:00:00Z",
  can_manage: true,
};

beforeEach(() => {
  vi.spyOn(api, "me").mockResolvedValue(ADMIN);
  vi.spyOn(api, "adminUsers").mockResolvedValue({
    count: 1,
    next: null,
    previous: null,
    results: [STUDENT],
  });
});

afterEach(() => vi.restoreAllMocks());

describe("UsersPage", () => {
  it("says why setting a password failed when the reason is not about the password", async () => {
    // The account became another administrator's after the list loaded.
    vi.spyOn(api, "adminSetPassword").mockRejectedValue(
      new ApiError(403, "You may not modify this account.", {
        detail: "You may not modify this account.",
      }),
    );

    render(
      <MemoryRouter initialEntries={["/admin/users"]}>
        <AuthProvider>
          <ToastProvider>
            <App />
          </ToastProvider>
        </AuthProvider>
      </MemoryRouter>,
    );

    // Generous: the admin screens are a lazily loaded chunk.
    await userEvent.click(
      await screen.findByRole("button", { name: "Set password" }, { timeout: 15_000 }),
    );
    const dialog = await screen.findByRole("dialog", { name: /Set a new password for Asha Rao/ });
    await userEvent.type(within(dialog).getByLabelText("New password"), "a-new-passphrase");
    await userEvent.click(within(dialog).getByRole("button", { name: "Set password" }));

    expect(await within(dialog).findByText("You may not modify this account.")).toBeInTheDocument();
  });
});
