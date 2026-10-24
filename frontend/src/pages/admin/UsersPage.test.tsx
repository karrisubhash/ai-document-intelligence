import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, api } from "../../api/client";
import type { AdminUser, Me, SignupRequest } from "../../api/types";
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
    create_collections: false,
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

const APPLICANT: SignupRequest = {
  id: 7,
  username: "nia",
  email: "nia@example.edu",
  first_name: "Nia",
  last_name: "Okafor",
  department: "Computer Science",
  role: "STUDENT",
  note: "Roll number CS-2031.",
  status: "PENDING",
  created_at: "2026-10-24T09:00:00Z",
  reviewed_by: null,
  reviewed_at: null,
  decision_note: "",
  user: null,
};

function page<T>(results: T[]) {
  return { count: results.length, next: null, previous: null, results };
}

function renderUsers(path = "/admin/users") {
  return render(
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
  vi.spyOn(api, "me").mockResolvedValue(ADMIN);
  vi.spyOn(api, "adminUsers").mockResolvedValue(page([STUDENT]));
  vi.spyOn(api, "adminSignupRequests").mockResolvedValue(page([APPLICANT]));
  vi.spyOn(api, "adminRoles").mockResolvedValue([
    { value: "FACULTY", label: "Faculty" },
    { value: "STAFF", label: "Staff" },
    { value: "STUDENT", label: "Student" },
  ]);
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

describe("sign-up requests", () => {
  const SYSTEM_ADMIN: Me = {
    ...ADMIN,
    username: "root",
    role: "SYSTEM_ADMIN",
    department: "",
    capabilities: { ...ADMIN.capabilities, administer_departments: true, view_system_status: true },
  };

  async function openApprove(name = "Nia Okafor") {
    await userEvent.click(
      await screen.findByRole("button", { name: `Approve ${name}` }, { timeout: 15_000 }),
    );
    return screen.findByRole("dialog", { name: new RegExp(`Approve ${name}`) });
  }

  it("counts who is waiting on the tab", async () => {
    renderUsers();

    const tab = await screen.findByRole("tab", { name: /Sign-up requests/ }, { timeout: 15_000 });
    expect(await within(tab).findByText("1")).toBeInTheDocument();
    // Waiting requests only - not every request ever decided.
    expect(api.adminSignupRequests).toHaveBeenCalledWith({ status: "PENDING" });
  });

  it("approves with the role chosen, leaving the department to the server", async () => {
    const approve = vi
      .spyOn(api, "adminApproveSignup")
      .mockResolvedValue({ ...APPLICANT, status: "APPROVED", user: 9 });
    renderUsers("/admin/users?tab=requests");

    expect(await screen.findByText("Roll number CS-2031.", {}, { timeout: 15_000 })).toBeInTheDocument();
    const listed = vi.mocked(api.adminSignupRequests).mock.calls.length;
    const dialog = await openApprove();
    // What they asked for, until the reviewer says otherwise.
    expect(within(dialog).getByLabelText(/^Role/)).toHaveValue("STUDENT");
    await userEvent.selectOptions(within(dialog).getByLabelText(/^Role/), "STAFF");
    await userEvent.click(within(dialog).getByRole("button", { name: "Approve and create account" }));

    // A department admin's approvals land in their own department.
    expect(approve).toHaveBeenCalledWith(7, { role: "STAFF" });
    expect(await screen.findByText("Approved. nia can sign in now.")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    // The list and the tab's count are both asked again.
    expect(vi.mocked(api.adminSignupRequests).mock.calls.length).toBe(listed + 2);
  });

  it("makes a system admin choose a department for a request that named none", async () => {
    vi.mocked(api.me).mockResolvedValue(SYSTEM_ADMIN);
    vi.mocked(api.adminSignupRequests).mockResolvedValue(page([{ ...APPLICANT, department: "" }]));
    vi.spyOn(api, "departments").mockResolvedValue([
      { id: 1, name: "Chemistry", code: "CHE" },
      { id: 2, name: "Computer Science", code: "CSE" },
    ]);
    vi.mocked(api.adminRoles).mockResolvedValue([
      { value: "SYSTEM_ADMIN", label: "System Admin" },
      { value: "DEPARTMENT_ADMIN", label: "Department Admin" },
      { value: "FACULTY", label: "Faculty" },
      { value: "STAFF", label: "Staff" },
      { value: "STUDENT", label: "Student" },
    ]);
    const approve = vi
      .spyOn(api, "adminApproveSignup")
      .mockResolvedValue({ ...APPLICANT, status: "APPROVED", user: 9 });
    renderUsers("/admin/users?tab=requests");

    const dialog = await openApprove();
    const button = within(dialog).getByRole("button", { name: "Approve and create account" });
    // Not approved into no department by default.
    expect(button).toBeDisabled();
    // Never an administrator, from an approval.
    const roles = within(within(dialog).getByLabelText(/^Role/)).getAllByRole("option");
    expect(roles.map((option) => option.textContent)).toEqual(["Faculty", "Staff", "Student"]);

    await within(dialog).findByRole("option", { name: "Chemistry" });
    await userEvent.selectOptions(within(dialog).getByLabelText(/^Department/), "Chemistry");
    await userEvent.click(button);

    expect(approve).toHaveBeenCalledWith(7, { role: "STUDENT", department: "Chemistry" });
  });

  it("warns when an account already uses the address", async () => {
    vi.mocked(api.adminSignupRequests).mockResolvedValue(
      page([{ ...APPLICANT, email_in_use: ["asha"] }]),
    );
    renderUsers("/admin/users?tab=requests");

    const dialog = await openApprove();

    expect(within(dialog).getByText("An account already uses this email address")).toBeInTheDocument();
    expect(within(dialog).getByText(/asha\./)).toBeInTheDocument();
  });

  it("refreshes a request decided elsewhere, and says so", async () => {
    vi.spyOn(api, "adminApproveSignup").mockRejectedValue(
      new ApiError(409, "This request was already approved.", {
        detail: "This request was already approved.",
      }),
    );
    renderUsers("/admin/users?tab=requests");

    const dialog = await openApprove();
    const listed = vi.mocked(api.adminSignupRequests).mock.calls.length;
    await userEvent.click(within(dialog).getByRole("button", { name: "Approve and create account" }));

    expect(await within(dialog).findByText("This request was already approved.")).toBeInTheDocument();
    // The stale row and the count are fetched again.
    expect(vi.mocked(api.adminSignupRequests).mock.calls.length).toBe(listed + 2);
  });

  it("steps back a page when deciding empties the last one", async () => {
    const pageOne = { count: 51, next: null, previous: null, results: [APPLICANT] };
    const pageTwo = { count: 51, next: null, previous: "x", results: [{ ...APPLICANT, id: 8 }] };
    vi.mocked(api.adminSignupRequests).mockImplementation(async (params = {}) =>
      params.page === 2 ? pageTwo : pageOne,
    );
    vi.spyOn(api, "adminRejectSignup").mockResolvedValue({ ...APPLICANT, id: 8, status: "REJECTED" });
    renderUsers("/admin/users?tab=requests");

    await userEvent.click(await screen.findByRole("button", { name: /Next/ }, { timeout: 15_000 }));
    await waitFor(() =>
      expect(api.adminSignupRequests).toHaveBeenLastCalledWith(expect.objectContaining({ page: 2 })),
    );
    await userEvent.click(await screen.findByRole("button", { name: "Decline Nia Okafor" }));
    const dialog = await screen.findByRole("dialog", { name: /Decline Nia Okafor/ });
    const before = vi.mocked(api.adminSignupRequests).mock.calls.length;
    await userEvent.click(within(dialog).getByRole("button", { name: "Decline request" }));

    // Only what was asked after the decline: the list's first page, and
    // never again the page the decline emptied (the server would 404).
    const after = () =>
      vi.mocked(api.adminSignupRequests).mock.calls.slice(before).map(([params]) => params ?? {});
    await waitFor(() =>
      expect(after()).toContainEqual(expect.objectContaining({ status: "PENDING", page: 1 })),
    );
    expect(after().some((params) => params.page === 2)).toBe(false);
  });

  it("gives focus back to the button that opened a dialog closed without a decision", async () => {
    renderUsers("/admin/users?tab=requests");

    const opener = await screen.findByRole("button", { name: "Approve Nia Okafor" }, { timeout: 15_000 });
    await userEvent.click(opener);
    const dialog = await screen.findByRole("dialog", { name: /Approve Nia Okafor/ });
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.getByRole("button", { name: "Approve Nia Okafor" })).toHaveFocus());
  });

  it("declines with the reason given", async () => {
    const reject = vi
      .spyOn(api, "adminRejectSignup")
      .mockResolvedValue({ ...APPLICANT, status: "REJECTED" });
    renderUsers("/admin/users?tab=requests");

    await userEvent.click(
      await screen.findByRole("button", { name: "Decline Nia Okafor" }, { timeout: 15_000 }),
    );
    const dialog = await screen.findByRole("dialog", { name: /Decline Nia Okafor/ });
    await userEvent.type(within(dialog).getByLabelText(/^Reason/), "Use your college email.");
    await userEvent.click(within(dialog).getByRole("button", { name: "Decline request" }));

    expect(reject).toHaveBeenCalledWith(7, "Use your college email.");
    expect(await screen.findByText("Request from nia declined.")).toBeInTheDocument();
  });

  it("declines every waiting request matching a search, within the count it showed", async () => {
    const bulk = vi
      .spyOn(api, "adminBulkRejectSignups")
      .mockImplementation(async (target) =>
        target.preview
          ? { rejected: [], not_found: [], remaining: 2, as_of: "2026-10-24T10:00:00Z" }
          : { rejected: [7, 8], not_found: [], remaining: 0, as_of: "2026-10-24T10:00:00Z" },
      );
    renderUsers("/admin/users?tab=requests");

    await userEvent.type(
      await screen.findByLabelText("Search requests", {}, { timeout: 15_000 }),
      "spam",
    );
    await userEvent.click(await screen.findByRole("button", { name: "Decline all matching…" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Decline 2 waiting requests matching “spam”?",
    });
    await userEvent.type(within(dialog).getByLabelText(/^Reason/), "Junk.");
    await userEvent.click(within(dialog).getByRole("button", { name: "Decline 2 requests" }));

    expect(bulk).toHaveBeenLastCalledWith(
      { all: true, search: "spam", as_of: "2026-10-24T10:00:00Z" },
      "Junk.",
    );
    expect(await screen.findByText("Declined 2 requests.")).toBeInTheDocument();
  });
});
