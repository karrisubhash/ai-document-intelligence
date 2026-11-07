import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import {
  announceArrivals,
  attentionBadge,
  notificationsApi,
  useNotificationArrivals,
  type AppNotification,
  type Attention,
} from "../api/notifications";
import type { Capabilities, Me } from "../api/types";
import App from "../App";
import { AuthProvider } from "../auth/AuthContext";
import NotificationBell from "./NotificationBell";
import { ToastProvider } from "./ui";

const NOW = new Date().toISOString();
const EARLIER = new Date(Date.now() - 5 * 60_000).toISOString();

function note(overrides: Partial<AppNotification> = {}): AppNotification {
  return {
    id: 1,
    kind: "document.ready",
    title: "“Fee structure” is ready",
    body: "It has been read and indexed.",
    url: "/documents/4",
    created_at: NOW,
    updated_at: NOW,
    read_at: null,
    ...overrides,
  };
}

function page(results: AppNotification[], next: string | null = null) {
  return { count: results.length, next, previous: null, results };
}

function Where() {
  const location = useLocation();
  return <p data-testid="where">{location.pathname}</p>;
}

function renderBell() {
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <NotificationBell />
      <Routes>
        <Route path="*" element={<Where />} />
      </Routes>
    </MemoryRouter>,
  );
}

function setVisibility(state: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", { value: state, configurable: true });
  document.dispatchEvent(new Event("visibilitychange"));
}

afterEach(() => {
  setVisibility("visible");
  vi.useRealTimers();
});

describe("notification bell", () => {
  it("shows the unread count and says it in its name", async () => {
    vi.spyOn(notificationsApi, "summary").mockResolvedValue({ unread: 3, latest_at: NOW });
    renderBell();

    const bell = await screen.findByRole("button", { name: "Notifications, 3 unread" });
    expect(bell).toHaveTextContent("3");
    expect(bell).toHaveAttribute("aria-expanded", "false");
  });

  it("caps a large count", async () => {
    vi.spyOn(notificationsApi, "summary").mockResolvedValue({ unread: 250, latest_at: NOW });
    renderBell();
    expect(await screen.findByRole("button", { name: "Notifications, 250 unread" })).toHaveTextContent(
      "99+",
    );
  });

  it("opens a panel with the latest news, unread first in weight", async () => {
    vi.spyOn(notificationsApi, "summary").mockResolvedValue({ unread: 1, latest_at: NOW });
    vi.spyOn(notificationsApi, "list").mockResolvedValue(
      page([
        note(),
        note({
          id: 2,
          kind: "signup.requested",
          title: "Nia Okafor asked for an account",
          url: "/admin/users?tab=requests",
          read_at: EARLIER,
          updated_at: EARLIER,
        }),
      ]),
    );
    renderBell();

    await userEvent.click(await screen.findByRole("button", { name: /Notifications/ }));

    const panel = await screen.findByRole("dialog", { name: "Notifications" });
    const links = await within(panel).findAllByRole("link");
    expect(links).toHaveLength(2);
    expect(links[0]).toHaveTextContent("“Fee structure” is ready");
    expect(links[0]).toHaveTextContent(/Unread\. Document ready/);
    expect(links[0]).toHaveClass("unread");
    expect(links[1]).not.toHaveClass("unread");
    expect(links[1]).toHaveTextContent("Sign-up request");
    expect(screen.getByRole("button", { name: /Notifications/ })).toHaveAttribute("aria-expanded", "true");
    expect(panel).toHaveFocus();
  });

  it("marks a notification read and goes where it points", async () => {
    vi.spyOn(notificationsApi, "summary").mockResolvedValue({ unread: 1, latest_at: NOW });
    vi.spyOn(notificationsApi, "list").mockResolvedValue(page([note()]));
    const markRead = vi.spyOn(notificationsApi, "markRead").mockResolvedValue(undefined);
    renderBell();

    await userEvent.click(await screen.findByRole("button", { name: "Notifications, 1 unread" }));
    await userEvent.click(await screen.findByRole("link", { name: /Fee structure/ }));

    expect(markRead).toHaveBeenCalledWith(1);
    expect(screen.getByTestId("where")).toHaveTextContent("/documents/4");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Notifications" })).toBeInTheDocument();
  });

  it("marks everything read up to the newest news it has shown", async () => {
    const summary = vi
      .spyOn(notificationsApi, "summary")
      .mockResolvedValueOnce({ unread: 2, latest_at: NOW })
      .mockResolvedValue({ unread: 0, latest_at: NOW });
    vi.spyOn(notificationsApi, "list").mockResolvedValue(page([note(), note({ id: 2, title: "Second" })]));
    const markAll = vi.spyOn(notificationsApi, "markAllRead").mockResolvedValue({ updated: 2 });
    renderBell();

    await userEvent.click(await screen.findByRole("button", { name: "Notifications, 2 unread" }));
    await userEvent.click(await screen.findByRole("button", { name: "Mark all as read" }));

    expect(markAll).toHaveBeenCalledWith(NOW);
    await waitFor(() => expect(screen.getByRole("button", { name: "Notifications" })).toBeInTheDocument());
    expect(summary).toHaveBeenCalledTimes(2);
    for (const link of screen.getAllByRole("link")) expect(link).not.toHaveClass("unread");
    expect(screen.queryByRole("button", { name: "Mark all as read" })).not.toBeInTheDocument();
  });

  it("has a designed empty state", async () => {
    vi.spyOn(notificationsApi, "summary").mockResolvedValue({ unread: 0, latest_at: null });
    vi.spyOn(notificationsApi, "list").mockResolvedValue(page([]));
    renderBell();

    await userEvent.click(await screen.findByRole("button", { name: "Notifications" }));

    expect(await screen.findByText("You are all caught up")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Mark all as read" })).not.toBeInTheDocument();
  });

  it("closes on Escape and gives focus back to the bell", async () => {
    vi.spyOn(notificationsApi, "summary").mockResolvedValue({ unread: 0, latest_at: null });
    vi.spyOn(notificationsApi, "list").mockResolvedValue(page([note({ read_at: NOW })]));
    renderBell();
    const bell = await screen.findByRole("button", { name: "Notifications" });

    await userEvent.click(bell);
    await screen.findByRole("link", { name: /Fee structure/ });
    await userEvent.keyboard("{Escape}");

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(bell).toHaveFocus();
  });

  it("loads older news on request", async () => {
    vi.spyOn(notificationsApi, "summary").mockResolvedValue({ unread: 0, latest_at: NOW });
    const list = vi
      .spyOn(notificationsApi, "list")
      .mockResolvedValueOnce(page([note({ read_at: NOW })], "/api/v1/notifications/?page=2"))
      .mockResolvedValueOnce(page([note({ id: 9, title: "Older news", read_at: NOW })]));
    renderBell();

    await userEvent.click(await screen.findByRole("button", { name: "Notifications" }));
    await userEvent.click(await screen.findByRole("button", { name: "Show older" }));

    expect(await screen.findByRole("link", { name: /Older news/ })).toBeInTheDocument();
    expect(list).toHaveBeenLastCalledWith({ page: 2 });
    expect(screen.queryByRole("button", { name: "Show older" })).not.toBeInTheDocument();
  });

  it("stays quiet when the summary cannot be fetched", async () => {
    const summary = vi.spyOn(notificationsApi, "summary").mockRejectedValue(new Error("down"));
    renderBell();

    await waitFor(() => expect(summary).toHaveBeenCalled());
    expect(screen.getByRole("button", { name: "Notifications" })).toBeInTheDocument();
  });

  it("tells the open screens when news arrives, and says so to a screen reader", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.spyOn(notificationsApi, "summary")
      .mockResolvedValueOnce({ unread: 0, latest_at: EARLIER })
      .mockResolvedValue({ unread: 1, latest_at: NOW });
    vi.spyOn(notificationsApi, "list").mockResolvedValue(
      page([note(), note({ id: 2, title: "Old", updated_at: EARLIER, read_at: EARLIER })]),
    );
    const heard = vi.fn();
    function Listener() {
      useNotificationArrivals(heard);
      return null;
    }
    render(
      <MemoryRouter>
        <NotificationBell />
        <Listener />
      </MemoryRouter>,
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(heard).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });

    expect(heard).toHaveBeenCalledTimes(1);
    expect(heard.mock.calls[0][0].map((item: AppNotification) => item.id)).toEqual([1]);
    expect(screen.getByRole("status")).toHaveTextContent("New notification: “Fee structure” is ready");
  });

  it("polls once a minute while visible, not at all while hidden, and again on return", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const summary = vi.spyOn(notificationsApi, "summary").mockResolvedValue({ unread: 0, latest_at: null });
    renderBell();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(summary).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(summary).toHaveBeenCalledTimes(2);

    setVisibility("hidden");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(180_000);
    });
    expect(summary).toHaveBeenCalledTimes(2);

    act(() => setVisibility("visible"));
    expect(summary).toHaveBeenCalledTimes(3);

    // Focus right after: folded into the call just made.
    act(() => {
      window.dispatchEvent(new Event("focus"));
    });
    expect(summary).toHaveBeenCalledTimes(3);
  });
});

describe("attention badges", () => {
  const NONE: Capabilities = {
    administer_users: false,
    administer_departments: false,
    view_audit_log: false,
    view_operations: false,
    view_system_status: false,
    upload_to_any_department: false,
    create_collections: false,
  };
  const ADMIN: Capabilities = { ...NONE, administer_users: true, view_audit_log: true, view_operations: true };

  function me(capabilities: Capabilities): Me {
    return {
      id: 1,
      username: "asha",
      email: "",
      first_name: "Asha",
      last_name: "Rao",
      is_superuser: false,
      role: capabilities.administer_users ? "DEPARTMENT_ADMIN" : "STUDENT",
      department: "Physics",
      capabilities,
    };
  }

  const COUNTS: Attention = {
    pending_signups: 2,
    failed_documents: 3,
    quarantined_documents: 1,
    stalled_jobs: 1,
  };

  beforeEach(() => {
    vi.spyOn(api, "dashboard").mockResolvedValue({
      documents: { total: 0, by_status: {} },
      recent_documents: [],
      needs_attention: [],
      recent_conversations: [],
    });
    vi.spyOn(api, "departments").mockResolvedValue([]);
    vi.spyOn(notificationsApi, "summary").mockResolvedValue({ unread: 0, latest_at: null });
  });

  function renderApp() {
    return render(
      <MemoryRouter>
        <AuthProvider>
          <ToastProvider>
            <App />
          </ToastProvider>
        </AuthProvider>
      </MemoryRouter>,
    );
  }

  it("counts on Users and Processing, without changing their names", async () => {
    vi.spyOn(api, "me").mockResolvedValue(me(ADMIN));
    vi.spyOn(notificationsApi, "attention").mockResolvedValue(COUNTS);
    renderApp();
    const nav = await screen.findByRole("complementary", { name: "Main navigation" });

    const users = within(nav).getByRole("link", { name: "Users" });
    await waitFor(() => expect(users).toHaveAccessibleDescription("2 sign-up requests waiting"));
    expect(users).toHaveTextContent("Users2");
    const processing = within(nav).getByRole("link", { name: "Processing" });
    expect(processing).toHaveAccessibleDescription("3 failed documents (1 quarantined), 1 stalled run");
    expect(processing).toHaveTextContent("4");
    // Exactly one link answers to each name, as the e2e specs require.
    expect(screen.getAllByRole("link", { name: /Users/ })).toHaveLength(1);
    expect(screen.getAllByRole("link", { name: /Processing/ })).toHaveLength(1);
  });

  it("shows nothing when nothing is waiting", async () => {
    vi.spyOn(api, "me").mockResolvedValue(me(ADMIN));
    const attention = vi.spyOn(notificationsApi, "attention").mockResolvedValue({
      pending_signups: 0,
      failed_documents: 0,
      quarantined_documents: 0,
      stalled_jobs: 0,
    });
    renderApp();
    const nav = await screen.findByRole("complementary", { name: "Main navigation" });
    await waitFor(() => expect(attention).toHaveBeenCalled());

    expect(within(nav).getByRole("link", { name: "Users" })).not.toHaveAccessibleDescription(/waiting/);
    expect(nav.querySelector(".nav-badge")).toBeNull();
  });

  it("never asks for counts for someone who is not an administrator", async () => {
    vi.spyOn(api, "me").mockResolvedValue(me(NONE));
    const attention = vi.spyOn(notificationsApi, "attention");
    renderApp();
    await screen.findByRole("complementary", { name: "Main navigation" });
    await screen.findByRole("button", { name: "Notifications" });

    expect(attention).not.toHaveBeenCalled();
  });

  it("refreshes the documents list when an upload's news arrives", async () => {
    vi.spyOn(api, "me").mockResolvedValue(me(NONE));
    const documents = vi.spyOn(api, "documents").mockResolvedValue({
      count: 0,
      next: null,
      previous: null,
      results: [],
    });
    render(
      <MemoryRouter initialEntries={["/documents"]}>
        <AuthProvider>
          <ToastProvider>
            <App />
          </ToastProvider>
        </AuthProvider>
      </MemoryRouter>,
    );
    await screen.findByText("No documents yet");
    const before = documents.mock.calls.length;

    act(() => announceArrivals([note({ kind: "signup.requested" })]));
    expect(documents).toHaveBeenCalledTimes(before);

    act(() => announceArrivals([note({ kind: "document.failed" })]));
    await waitFor(() => expect(documents).toHaveBeenCalledTimes(before + 1));
  });

  it("describes the counts in words", () => {
    expect(attentionBadge("signups", { ...COUNTS, pending_signups: 1 })?.description).toBe(
      "1 sign-up request waiting",
    );
    expect(
      attentionBadge("processing", { ...COUNTS, failed_documents: 0, quarantined_documents: 0 })?.description,
    ).toBe("1 stalled run");
    expect(attentionBadge("processing", null)).toBeNull();
  });
});
