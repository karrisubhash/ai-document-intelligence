import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "./api/client";
import type { Dashboard, Me, Paginated } from "./api/types";
import App from "./App";
import { AuthProvider } from "./auth/AuthContext";
import { StatTile, ToastProvider } from "./components/ui";
import { lazy, prefetchRoute } from "./lib/lazy";

const NONE = {
  administer_users: false,
  administer_departments: false,
  view_audit_log: false,
  view_operations: false,
  view_system_status: false,
  upload_to_any_department: false,
  create_collections: false,
};

function me(overrides: Partial<Me> = {}): Me {
  return {
    id: 1,
    username: "asha",
    email: "",
    first_name: "Asha",
    last_name: "Rao",
    is_superuser: false,
    role: "STUDENT",
    department: "Physics",
    capabilities: NONE,
    ...overrides,
  };
}

const DASHBOARD: Dashboard = {
  documents: { total: 12, by_status: { COMPLETED: 10, PROCESSING: 0, FAILED: 2 } },
  recent_documents: [],
  needs_attention: [],
  recent_conversations: [],
};

function page<T>(results: T[]): Paginated<T> {
  return { count: results.length, next: null, previous: null, results };
}

function renderAt(path: string) {
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
  vi.spyOn(api, "me").mockResolvedValue(me());
  vi.spyOn(api, "dashboard").mockResolvedValue(DASHBOARD);
  vi.spyOn(api, "departments").mockResolvedValue([]);
  vi.spyOn(api, "documents").mockResolvedValue(page([]));
});

describe("top bar", () => {
  it("names a top-level screen without the sidebar group in front of it", async () => {
    renderAt("/documents");
    const topbar = (await screen.findByRole("banner")) as HTMLElement;
    await waitFor(() => expect(within(topbar).getByText("Documents")).toBeInTheDocument());
    expect(within(topbar).queryByText("Workspace")).toBeNull();
  });

  it("chooses the theme in one place: the user menu, not a top-bar toggle", async () => {
    renderAt("/");
    await screen.findByRole("heading", { name: /^Welcome,/ });
    expect(screen.queryByRole("button", { name: /Switch to the (dark|light) theme/ })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: /Asha Rao/ }));
    expect(screen.getByRole("menuitemradio", { name: "Dark" })).toBeInTheDocument();
    expect(screen.getByRole("menuitemradio", { name: "System" })).toBeInTheDocument();
  });
});

describe("keyboard", () => {
  it("shows the shortcuts on ?, with quick find among them", async () => {
    renderAt("/");
    await screen.findByRole("heading", { name: /^Welcome,/ });
    await userEvent.keyboard("?");
    const sheet = await screen.findByRole("dialog", { name: "Keyboard shortcuts" });
    expect(within(sheet).getByText(/Quick find: open a document/)).toBeInTheDocument();
    expect(within(sheet).getByText("K")).toBeInTheDocument();
  });

  it("types ? and / into a field instead of acting on them", async () => {
    renderAt("/");
    const question = await screen.findByPlaceholderText("Ask a question about your documents…");
    await userEvent.click(question);
    await userEvent.keyboard("what? and/or");
    expect(question).toHaveValue("what? and/or");
    expect(screen.queryByRole("dialog", { name: "Keyboard shortcuts" })).toBeNull();
  });

  it("goes to the page's search field on /", async () => {
    renderAt("/documents");
    const search = await screen.findByRole("searchbox", { name: "Search titles and file names" });
    act(() => document.getElementById("main")?.focus());
    await userEvent.keyboard("/");
    expect(search).toHaveFocus();
    expect(search).toHaveValue("");
  });

  it("offers the shortcuts from quick find and the user menu", async () => {
    renderAt("/");
    await screen.findByRole("heading", { name: /^Welcome,/ });
    await userEvent.click(screen.getByRole("button", { name: /Asha Rao/ }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Keyboard shortcuts" }));
    expect(await screen.findByRole("dialog", { name: "Keyboard shortcuts" })).toBeInTheDocument();
  });
});

describe("dashboard", () => {
  it("tells a system administrator they see everything", async () => {
    vi.spyOn(api, "me").mockResolvedValue(me({ role: "SYSTEM_ADMIN", department: "" }));
    renderAt("/");
    expect(await screen.findByText("You can see every document, in every department.")).toBeInTheDocument();
    expect(screen.queryByText(/Ask an administrator to assign you a department/)).toBeNull();
  });

  it("still tells someone without a department how to get one", async () => {
    vi.spyOn(api, "me").mockResolvedValue(me({ department: "" }));
    renderAt("/");
    expect(await screen.findByText(/Ask an administrator to assign you a department/)).toBeInTheDocument();
  });

  it("shows skeleton figures until the counts arrive, then the counts", async () => {
    let deliver: (value: Dashboard) => void = () => undefined;
    vi.spyOn(api, "dashboard").mockImplementation(
      () => new Promise<Dashboard>((resolve) => (deliver = resolve)),
    );
    renderAt("/");
    const summary = await screen.findByRole("region", { name: "Document summary" });
    expect(within(summary).queryByText("—")).toBeNull();
    expect(summary.querySelectorAll('[aria-busy="true"]')).toHaveLength(4);
    await act(async () => deliver(DASHBOARD));
    expect(within(summary).getByText("12")).toBeInTheDocument();
    expect(summary.querySelectorAll('[aria-busy="true"]')).toHaveLength(0);
  });
});

describe("stat tile", () => {
  it("shows a dash, not a skeleton, when the figure could not be loaded", () => {
    render(<StatTile label="Ready" value={undefined} loading={false} caption="Unavailable" />);
    expect(screen.getByText("—")).toBeInTheDocument();
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
  });
});

describe("route prefetch", () => {
  it("loads a screen's code once when its link is pointed at, and reuses it", async () => {
    const load = vi.fn(() => Promise.resolve({ default: () => <p>Prefetched</p> }));
    const Page = lazy(load, "/prefetch-test");
    prefetchRoute("/prefetch-test");
    prefetchRoute("/prefetch-test");
    expect(load).toHaveBeenCalledTimes(1);
    render(<Page />);
    expect(await screen.findByText("Prefetched")).toBeInTheDocument();
    expect(load).toHaveBeenCalledTimes(1);
  });
});
