import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "./api/client";
import type { ChatResponse, DocumentSummary, Me, Paginated } from "./api/types";
import App from "./App";
import { AuthProvider } from "./auth/AuthContext";
import { Menu, MenuItem, ToastProvider } from "./components/ui";
import { setTheme, useTheme } from "./lib/theme";

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

function page<T>(results: T[]): Paginated<T> {
  return { count: results.length, next: null, previous: null, results };
}

function document(id: number, title: string): DocumentSummary {
  return {
    id,
    title,
    document_type: "REGULATION",
    department: "Physics",
    owner: 1,
    owner_username: "asha",
    status: "COMPLETED",
    current_version: null,
    is_archived: false,
    archived_at: null,
    created_at: "2026-10-05T10:00:00Z",
    updated_at: "2026-10-05T10:00:00Z",
    can_manage: false,
  };
}

function answer(conversationId: number, text: string): ChatResponse {
  return {
    conversation_id: conversationId,
    question: "q",
    resolved_question: "",
    answer: text,
    abstained: false,
    abstention_reason: "",
    citations: [],
    grounding: { support_ratio: 1, failures: [] },
    retrieval: {
      retrieved: 1,
      reranked: 1,
      passages_used: 1,
      semantic: true,
      reranker: "lexical",
      suspected_injections: 0,
    },
    model: { name: "extractive-answerer", version: "0.1.0", generative: false },
    plan: { planned: false, steps: [], unanswered: [] },
    duration_ms: 10,
    timings: {},
    notes: [],
    message_id: 99,
  };
}

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{location.pathname + location.search}</output>;
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AuthProvider>
        <ToastProvider>
          <App />
        </ToastProvider>
      </AuthProvider>
      <LocationProbe />
    </MemoryRouter>,
  );
}

async function openQuickFind() {
  await userEvent.keyboard("{Control>}k{/Control}");
  return screen.findByRole("combobox", { name: "Search or jump to" });
}

beforeEach(() => {
  vi.spyOn(api, "me").mockResolvedValue(ME);
  vi.spyOn(api, "departments").mockResolvedValue([]);
  vi.spyOn(api, "collections").mockResolvedValue(page([]));
  vi.spyOn(api, "conversations").mockResolvedValue(page([]));
});

afterEach(() => vi.restoreAllMocks());

describe("menu button", () => {
  function renderMenu(onSelect = vi.fn()) {
    render(
      <>
        <Menu trigger="Open" triggerLabel="Actions">
          <MenuItem onSelect={onSelect}>First</MenuItem>
          <MenuItem onSelect={() => undefined}>Second</MenuItem>
        </Menu>
        <button type="button">Elsewhere</button>
      </>,
    );
    return onSelect;
  }

  it("moves focus into the menu, through it with the arrow keys, and back to the trigger on Escape", async () => {
    renderMenu();
    const trigger = screen.getByRole("button", { name: "Actions" });
    await userEvent.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("menuitem", { name: "First" })).toHaveFocus();

    await userEvent.keyboard("{ArrowDown}");
    expect(screen.getByRole("menuitem", { name: "Second" })).toHaveFocus();
    await userEvent.keyboard("{ArrowDown}");
    expect(screen.getByRole("menuitem", { name: "First" })).toHaveFocus();
    await userEvent.keyboard("{End}");
    expect(screen.getByRole("menuitem", { name: "Second" })).toHaveFocus();

    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("runs the chosen item and closes, and closes when focus moves elsewhere", async () => {
    const onSelect = renderMenu();
    await userEvent.click(screen.getByRole("button", { name: "Actions" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "First" }));
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Actions" }));
    act(() => screen.getByRole("button", { name: "Elsewhere" }).focus());
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });
});

describe("theme preference", () => {
  afterEach(() => setTheme("system"));

  function Probe() {
    const [preference] = useTheme();
    return <output data-testid="theme">{preference}</output>;
  }

  it("applies an explicit choice to <html>, remembers it, and lets System hand back to the OS", () => {
    act(() => setTheme("dark"));
    expect(window.document.documentElement).toHaveAttribute("data-theme", "dark");
    expect(window.localStorage.getItem("adi-theme")).toBe("dark");

    act(() => setTheme("system"));
    expect(window.document.documentElement).not.toHaveAttribute("data-theme");
    expect(window.localStorage.getItem("adi-theme")).toBeNull();
  });

  it("follows a choice made in another tab", () => {
    render(<Probe />);
    expect(screen.getByTestId("theme")).toHaveTextContent("system");

    window.localStorage.setItem("adi-theme", "light");
    act(() => {
      window.dispatchEvent(new StorageEvent("storage", { key: "adi-theme" }));
    });
    expect(screen.getByTestId("theme")).toHaveTextContent("light");
    expect(window.document.documentElement).toHaveAttribute("data-theme", "light");
  });
});

describe("quick find", () => {
  it("keeps the highlighted command when document results arrive above it", async () => {
    let deliver: (value: Paginated<DocumentSummary>) => void = () => undefined;
    vi.spyOn(api, "documents").mockImplementation((params = {}) =>
      params.search === "doc"
        ? new Promise((resolve) => (deliver = resolve))
        : Promise.resolve(page([])),
    );
    renderAt("/search");
    // The page first: its query box takes focus when it mounts, and jsdom
    // has no inert page behind a modal dialog to stop that.
    await screen.findByLabelText("Search documents");
    const input = await openQuickFind();

    await userEvent.type(input, "doc");
    // Ask, Search, then "Go to Documents".
    await userEvent.keyboard("{ArrowDown}{ArrowDown}");
    expect(screen.getByRole("option", { name: /^Documents/ })).toHaveAttribute("aria-selected", "true");

    await waitFor(() => expect(api.documents).toHaveBeenCalledWith({ search: "doc" }));
    act(() => deliver(page([document(5, "Docket rules")])));
    expect(await screen.findByRole("option", { name: /Docket rules/ })).toBeInTheDocument();
    // The result was inserted above the highlight; the highlight stayed put.
    expect(screen.getByRole("option", { name: /^Documents/ })).toHaveAttribute("aria-selected", "true");

    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(/^\/documents$/));
  });

  it("does not let the replaced Ask page's late answer pull the URL back to its conversation", async () => {
    const pending: ((response: ChatResponse) => void)[] = [];
    vi.spyOn(api, "chatStream").mockImplementation(
      () => new Promise<ChatResponse>((resolve) => pending.push(resolve)),
    );
    vi.spyOn(api, "documents").mockResolvedValue(page([]));
    renderAt("/ask");

    await userEvent.type(await screen.findByLabelText("Your question"), "First question{Enter}");
    await waitFor(() => expect(pending).toHaveLength(1));

    // Asked from quick find while the first answer is still on its way.
    const input = await openQuickFind();
    await userEvent.type(input, "Second question{Enter}");
    await waitFor(() => expect(pending).toHaveLength(2));
    expect(await screen.findByText("Second question")).toBeInTheDocument();

    await act(async () => pending[0](answer(42, "The first answer.")));
    expect(screen.getByTestId("location")).toHaveTextContent(/^\/ask$/);
    expect(screen.queryByText("The first answer.")).not.toBeInTheDocument();

    await act(async () => pending[1](answer(43, "The second answer.")));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(/^\/ask\/43$/));
    expect(screen.getByText("The second answer.")).toBeInTheDocument();
  });
});

describe("documents filters", () => {
  it("moves focus to the search field when Clear filters removes itself", async () => {
    vi.spyOn(api, "documents").mockResolvedValue(page([document(3, "Exam rules")]));
    renderAt("/documents?status=COMPLETED");
    await userEvent.click(await screen.findByRole("button", { name: "Clear filters" }));
    expect(screen.getByRole("searchbox", { name: "Search titles and file names" })).toHaveFocus();
    expect(screen.getByTestId("location")).toHaveTextContent(/^\/documents$/);
  });
});
