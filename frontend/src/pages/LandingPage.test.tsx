import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, api } from "../api/client";
import { demoApi } from "../api/demo";
import type { Capabilities, Me, SignupOptions } from "../api/types";
import App from "../App";
import { AuthProvider } from "../auth/AuthContext";
import { ToastProvider } from "../components/ui";

const SIGNUP_ON: SignupOptions = {
  enabled: true,
  departments: [{ id: 1, name: "Physics", code: "PHY" }],
  roles: [{ value: "STUDENT", label: "Student" }],
};
const SIGNUP_OFF: SignupOptions = { enabled: false, departments: [], roles: [] };

const NONE: Capabilities = {
  administer_users: false,
  administer_departments: false,
  view_audit_log: false,
  view_operations: false,
  view_system_status: false,
  upload_to_any_department: false,
  create_collections: false,
};

const GUEST: Me = {
  id: 9,
  username: "guest-7f3k2a9q",
  email: "",
  first_name: "Guest",
  last_name: "",
  is_superuser: false,
  role: "STUDENT",
  department: "Demo",
  capabilities: { ...NONE, is_guest: true },
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

function page<T>(results: T[]) {
  return { count: results.length, next: null, previous: null, results };
}

beforeEach(() => {
  vi.spyOn(api, "me").mockRejectedValue(new ApiError(401, "Not signed in.", null));
  vi.spyOn(api, "signupOptions").mockResolvedValue(SIGNUP_ON);
  vi.spyOn(demoApi, "status").mockResolvedValue({ enabled: true });
  vi.spyOn(api, "dashboard").mockResolvedValue({
    documents: { total: 3, by_status: { COMPLETED: 3 } },
    recent_documents: [],
    needs_attention: [],
    recent_conversations: [],
  });
  vi.spyOn(api, "departments").mockResolvedValue([]);
  vi.spyOn(api, "documents").mockResolvedValue(page([]));
  vi.spyOn(api, "conversations").mockResolvedValue(page([]));
  vi.spyOn(api, "collections").mockResolvedValue(page([]));
});

afterEach(() => vi.restoreAllMocks());

describe("the landing page", () => {
  it("pitches the product and keeps one sign-in form on the page", async () => {
    renderApp();

    expect(
      await screen.findByRole("heading", { level: 1, name: /Answers you can trace back to the page/ }),
    ).toBeInTheDocument();
    // The browser suite matches names as case-insensitive substrings:
    // exactly one heading and one button may say "Sign in", and one field
    // may be labelled "Password".
    expect(screen.getAllByRole("heading", { name: /sign in/i })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: /sign in/i })).toHaveLength(1);
    expect(screen.getAllByLabelText(/password/i)).toHaveLength(1);
    expect(screen.getAllByLabelText(/username/i)).toHaveLength(1);

    for (const name of [
      /Everything your documents know/,
      /From upload to cited answer/,
      /Built to be trusted/,
      /Questions, answered/,
    ]) {
      expect(screen.getByRole("heading", { level: 2, name })).toBeInTheDocument();
    }
    expect(screen.getByRole("img", { name: /How a document becomes a cited answer/ })).toBeInTheDocument();
    expect(screen.getByRole("contentinfo")).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "On this page" })).toBeInTheDocument();
  });

  it("offers the demo and an account request only when the server does", async () => {
    renderApp();
    expect(await screen.findByRole("button", { name: "Try the live demo" })).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "Request access" }).length).toBeGreaterThan(0);
    // The one exact "Request an account" link, which the sign-up tests use.
    expect(screen.getByRole("link", { name: "Request an account" })).toHaveAttribute(
      "href",
      "/signup",
    );
  });

  it("leaves both out when they are off", async () => {
    vi.mocked(demoApi.status).mockResolvedValue({ enabled: false });
    vi.mocked(api.signupOptions).mockResolvedValue(SIGNUP_OFF);
    renderApp();

    expect(await screen.findByText("Accounts are issued by your department administrator.")).toBeInTheDocument();
    await waitFor(() => expect(demoApi.status).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "Try the live demo" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Request access" })).not.toBeInTheDocument();
  });

  it("does not offer the demo when the server cannot be asked", async () => {
    vi.mocked(demoApi.status).mockRejectedValue(new ApiError(0, "down", null));
    renderApp();
    await screen.findByRole("heading", { name: "Sign in" });
    await waitFor(() => expect(demoApi.status).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "Try the live demo" })).not.toBeInTheDocument();
  });

  it("signs in from the hero", async () => {
    const login = vi.spyOn(api, "login").mockResolvedValue({
      ...GUEST,
      username: "asha",
      first_name: "Asha",
      last_name: "Rao",
      capabilities: NONE,
    });
    renderApp();

    await userEvent.type(await screen.findByLabelText(/^Username/), "asha");
    await userEvent.type(screen.getByLabelText(/^Password/), "secret-pass");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(login).toHaveBeenCalledWith("asha", "secret-pass");
    expect(await screen.findByRole("heading", { name: /^Welcome, Asha Rao/ })).toBeInTheDocument();
  });

  it("shows and hides the password on request", async () => {
    renderApp();
    const password = await screen.findByLabelText(/^Password/);
    const toggle = screen.getByRole("button", { name: "Show password" });
    await userEvent.type(password, "hunter2-long");

    expect(password).toHaveAttribute("type", "password");
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    await userEvent.click(toggle);
    expect(password).toHaveAttribute("type", "text");
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    expect(toggle).toHaveAttribute("aria-controls", password.id);
    await userEvent.click(toggle);
    expect(password).toHaveAttribute("type", "password");
  });

  it("lets the looping example be paused", async () => {
    renderApp();
    const pause = await screen.findByRole("button", { name: "Pause the example" });
    await userEvent.click(pause);
    expect(screen.getByRole("button", { name: "Play the example" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("starts the demo in one click and lands in a read-only workspace", async () => {
    const start = vi.spyOn(demoApi, "start").mockResolvedValue(GUEST);
    renderApp();

    await userEvent.click(await screen.findByRole("button", { name: "Try the live demo" }));

    expect(start).toHaveBeenCalledTimes(1);
    expect(await screen.findByRole("heading", { name: /^Welcome, Guest/ })).toBeInTheDocument();
    const banner = screen.getByRole("region", { name: "Demo workspace" });
    expect(within(banner).getByText("You're exploring a demo workspace.")).toBeInTheDocument();
    expect(await within(banner).findByRole("button", { name: /Request an account/ })).toBeInTheDocument();
  });

  it("says why the demo could not start", async () => {
    vi.spyOn(demoApi, "start").mockRejectedValue(
      new ApiError(429, "The demo is full right now.", {
        detail: "The demo is full right now.",
        code: "demo_full",
      }),
    );
    renderApp();
    await userEvent.click(await screen.findByRole("button", { name: "Try the live demo" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The demo is full right now.");
    expect(screen.getByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  });
});

describe("the focused sign-in page", () => {
  it("serves /login, and any address visited while signed out", async () => {
    renderApp("/login");
    expect(await screen.findByRole("heading", { level: 1, name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "What is Document Intelligence?" })).toHaveAttribute(
      "href",
      "/",
    );
  });

  it("keeps a deep link for after sign-in", async () => {
    renderApp("/documents");
    expect(await screen.findByRole("heading", { level: 1, name: "Sign in" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 1, name: /Answers you can trace/ })).toBeNull();
  });
});

describe("a demo guest", () => {
  beforeEach(() => {
    vi.mocked(api.me).mockResolvedValue(GUEST);
  });

  it("sees the banner and no upload on Documents", async () => {
    renderApp("/documents");
    expect(await screen.findByRole("region", { name: "Demo workspace" })).toBeInTheDocument();
    expect(await screen.findByText("No documents yet")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Upload/ })).not.toBeInTheDocument();
  });

  it("is not offered conversation management", async () => {
    vi.mocked(api.conversations).mockResolvedValue(
      page([
        {
          id: 4,
          title: "Attendance",
          department: "Demo",
          created_at: "2026-11-01T10:00:00Z",
          updated_at: "2026-11-01T10:00:00Z",
          message_count: 2,
        },
      ]),
    );
    renderApp("/ask");
    expect(await screen.findByRole("link", { name: /Attendance/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Select conversations" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Rename Attendance/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Delete Attendance/ })).not.toBeInTheDocument();
  });

  it("has no password to change", async () => {
    renderApp("/account");
    expect(await screen.findByText(/This account was made for your visit to the demo/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Change password" })).not.toBeInTheDocument();
  });

  it("is not shown the banner as a member", async () => {
    vi.mocked(api.me).mockResolvedValue({ ...GUEST, username: "asha", capabilities: NONE });
    renderApp("/documents");
    expect(await screen.findByRole("button", { name: "Upload document" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Demo workspace" })).not.toBeInTheDocument();
  });
});

describe("the samples endpoint client", () => {
  it("posts the department and returns the counts", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ queued: 16, skipped: 0 }), {
        status: 202,
        headers: { "Content-Type": "application/json" },
      }),
    );
    document.cookie = "csrftoken=t0ken; path=/";
    await expect(demoApi.loadSamples("Physics")).resolves.toEqual({ queued: 16, skipped: 0 });
    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe("/api/v1/admin/samples/");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({ department: "Physics" });
  });
});
