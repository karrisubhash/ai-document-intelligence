import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, api } from "../api/client";
import type { Me, SignupOptions } from "../api/types";
import App from "../App";
import { AuthProvider } from "../auth/AuthContext";
import { ToastProvider } from "../components/ui";

const OPTIONS: SignupOptions = {
  enabled: true,
  departments: [
    { id: 1, name: "Chemistry", code: "CHE" },
    { id: 2, name: "Physics", code: "PHY" },
  ],
  roles: [
    { value: "FACULTY", label: "Faculty" },
    { value: "STAFF", label: "Staff" },
    { value: "STUDENT", label: "Student" },
  ],
};

function renderApp(path = "/") {
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

// Anchored: a required field's label ends in " *", and "Password" must not
// also find "Confirm password".
async function fill(label: RegExp, value: string) {
  await userEvent.type(screen.getByLabelText(label), value);
}

beforeEach(() => {
  vi.spyOn(api, "me").mockRejectedValue(new ApiError(401, "Not signed in.", null));
  vi.spyOn(api, "signupOptions").mockResolvedValue(OPTIONS);
});

afterEach(() => vi.restoreAllMocks());

describe("sign-up", () => {
  it("is offered on the sign-in screen when the server allows it", async () => {
    renderApp();

    await userEvent.click(await screen.findByRole("link", { name: "Request an account" }));

    expect(await screen.findByRole("heading", { name: "Request an account" })).toBeInTheDocument();
  });

  it("is not offered when the server has it turned off", async () => {
    vi.mocked(api.signupOptions).mockResolvedValue({ enabled: false, departments: [], roles: [] });
    renderApp();

    expect(await screen.findByText("Accounts are issued by your department administrator.")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Request an account" })).not.toBeInTheDocument();
  });

  it("explains, rather than shows a dead form, when it is off and the address is typed in", async () => {
    vi.mocked(api.signupOptions).mockResolvedValue({ enabled: false, departments: [], roles: [] });
    renderApp("/signup");

    expect(await screen.findByText("Sign-up is not available here")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Send request" })).not.toBeInTheDocument();
  });

  it("sends the request and says what happens next", async () => {
    const signup = vi.spyOn(api, "signup").mockResolvedValue({ status: "PENDING" });
    renderApp("/signup");

    await screen.findByRole("heading", { name: "Request an account" });
    await fill(/^First name/, " Nia ");
    await fill(/^Last name/, "Okafor");
    await fill(/^Email/, "nia@example.edu");
    await fill(/^Username/, "nia.okafor");
    await userEvent.selectOptions(screen.getByLabelText(/^Department/), "Physics");
    await userEvent.selectOptions(screen.getByLabelText("I am"), "FACULTY");
    await fill(/^Password/, "a-long-enough-passphrase");
    await fill(/^Confirm password/, "a-long-enough-passphrase");
    await userEvent.click(screen.getByRole("button", { name: "Send request" }));

    expect(signup).toHaveBeenCalledWith({
      first_name: "Nia",
      last_name: "Okafor",
      email: "nia@example.edu",
      username: "nia.okafor",
      department: "Physics",
      role: "FACULTY",
      password: "a-long-enough-passphrase",
      note: "",
    });
    const heading = await screen.findByRole("heading", { name: "Request sent" });
    expect(heading).toHaveFocus();
    expect(screen.getByText("nia.okafor")).toBeInTheDocument();
  });

  it("sends no department for one that is not listed, and refuses to guess", async () => {
    const signup = vi.spyOn(api, "signup").mockResolvedValue({ status: "PENDING" });
    renderApp("/signup");

    await screen.findByRole("heading", { name: "Request an account" });
    await fill(/^First name/, "Nia");
    await fill(/^Email/, "nia@example.edu");
    await fill(/^Username/, "nia");
    await fill(/^Password/, "a-long-enough-passphrase");
    await fill(/^Confirm password/, "a-long-enough-passphrase");
    await userEvent.click(screen.getByRole("button", { name: "Send request" }));

    // Not chosen yet is not the same as "not listed".
    expect(await screen.findByText(/Choose your department/)).toBeInTheDocument();
    expect(signup).not.toHaveBeenCalled();

    await userEvent.selectOptions(
      screen.getByLabelText(/^Department/),
      "My department is not listed",
    );
    await fill(/^Password/, "a-long-enough-passphrase");
    await fill(/^Confirm password/, "a-long-enough-passphrase");
    await userEvent.click(screen.getByRole("button", { name: "Send request" }));

    expect(signup).toHaveBeenCalledWith(expect.objectContaining({ department: "" }));
  });

  it("catches mismatched passwords before sending anything", async () => {
    const signup = vi.spyOn(api, "signup");
    renderApp("/signup");

    await screen.findByRole("heading", { name: "Request an account" });
    await fill(/^First name/, "Nia");
    await fill(/^Email/, "nia@example.edu");
    await fill(/^Username/, "nia");
    await userEvent.selectOptions(screen.getByLabelText(/^Department/), "Physics");
    await fill(/^Password/, "a-long-enough-passphrase");
    await fill(/^Confirm password/, "a-different-passphrase");
    await userEvent.click(screen.getByRole("button", { name: "Send request" }));

    expect(await screen.findByText("The passwords do not match.")).toBeInTheDocument();
    expect(signup).not.toHaveBeenCalled();
  });

  it("puts the server's objection on the field it is about", async () => {
    vi.spyOn(api, "signup").mockRejectedValue(
      new ApiError(400, "username: This username is taken. Choose another.", {
        username: ["This username is taken. Choose another."],
      }),
    );
    renderApp("/signup");

    await screen.findByRole("heading", { name: "Request an account" });
    await fill(/^First name/, "Nia");
    await fill(/^Email/, "nia@example.edu");
    await fill(/^Username/, "taken");
    await userEvent.selectOptions(screen.getByLabelText(/^Department/), "Physics");
    await fill(/^Password/, "a-long-enough-passphrase");
    await fill(/^Confirm password/, "a-long-enough-passphrase");
    await userEvent.click(screen.getByRole("button", { name: "Send request" }));

    const username = screen.getByLabelText(/^Username/);
    expect(await screen.findByText("This username is taken. Choose another.")).toBeInTheDocument();
    expect(username).toHaveAttribute("aria-invalid", "true");
    // Typed again, not left in a form that failed.
    expect(screen.getByLabelText(/^Password/)).toHaveValue("");
  });
});

describe("signing in before approval", () => {
  it("tells an applicant their request is waiting, not that their password is wrong", async () => {
    vi.spyOn(api, "login").mockRejectedValue(
      new ApiError(403, "Your account request is waiting for an administrator's approval.", {
        detail: "Your account request is waiting for an administrator's approval.",
        code: "signup_pending",
      }),
    );
    renderApp();

    await userEvent.type(await screen.findByLabelText(/^Username/), "nia");
    await userEvent.type(screen.getByLabelText(/^Password/), "a-long-enough-passphrase");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByText("Waiting for approval")).toBeInTheDocument();
    expect(screen.getByText(/waiting for an administrator's approval/)).toBeInTheDocument();
    expect(screen.queryByText(/do not match an active account/)).not.toBeInTheDocument();
  });

  it("still treats any other refusal as a failed sign-in", async () => {
    vi.spyOn(api, "login").mockRejectedValue(
      new ApiError(403, "CSRF check failed: CSRF token missing.", {
        detail: "CSRF check failed: CSRF token missing.",
      }),
    );
    renderApp();

    await userEvent.type(await screen.findByLabelText(/^Username/), "nia");
    await userEvent.type(screen.getByLabelText(/^Password/), "whatever");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByText("CSRF check failed: CSRF token missing.")).toBeInTheDocument();
    expect(screen.queryByText("Waiting for approval")).not.toBeInTheDocument();
  });
});

describe("the dashboard", () => {
  const ADMIN: Me = {
    id: 1,
    username: "phy_admin",
    email: "",
    first_name: "Meera",
    last_name: "Iyer",
    is_superuser: false,
    role: "DEPARTMENT_ADMIN",
    department: "Physics",
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

  it("tells an administrator who is waiting to be let in", async () => {
    vi.mocked(api.me).mockResolvedValue(ADMIN);
    vi.spyOn(api, "dashboard").mockResolvedValue({
      documents: { total: 0, by_status: {} },
      recent_documents: [],
      needs_attention: [],
      recent_conversations: [],
      pending_signup_requests: 2,
    });
    renderApp();

    const alert = (await screen.findByText("2 sign-up requests waiting for you", {}, { timeout: 15_000 })).closest(
      ".alert",
    ) as HTMLElement;
    expect(within(alert).getByRole("link", { name: "Review" })).toHaveAttribute(
      "href",
      "/admin/users?tab=requests",
    );
  });
});
