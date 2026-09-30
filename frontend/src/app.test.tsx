import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, api } from "./api/client";
import type { Capabilities, Citation, Me } from "./api/types";
import App from "./App";
import { AuthProvider } from "./auth/AuthContext";
import { Answer, fromResponse, SourceDialog } from "./components/chat/Answer";
import { ToastProvider } from "./components/ui";

const NONE: Capabilities = {
  administer_users: false,
  administer_departments: false,
  view_audit_log: false,
  view_operations: false,
  view_system_status: false,
  upload_to_any_department: false,
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

beforeEach(() => {
  vi.spyOn(api, "dashboard").mockResolvedValue({
    documents: { total: 0, by_status: {} },
    recent_documents: [],
    needs_attention: [],
    recent_conversations: [],
  });
  vi.spyOn(api, "departments").mockResolvedValue([]);
});

describe("session", () => {
  it("shows the sign-in screen when there is no session", async () => {
    vi.spyOn(api, "me").mockRejectedValue(new ApiError(401, "no", null));
    renderApp();
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  });

  it("signs in and lands on the dashboard", async () => {
    vi.spyOn(api, "me").mockRejectedValue(new ApiError(401, "no", null));
    const login = vi.spyOn(api, "login").mockResolvedValue(me());
    renderApp();

    await userEvent.type(await screen.findByLabelText(/Username/), "asha");
    await userEvent.type(screen.getByLabelText(/Password/), "secret-pass");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(login).toHaveBeenCalledWith("asha", "secret-pass");
    expect(await screen.findByRole("heading", { name: /Welcome, Asha Rao/ })).toBeInTheDocument();
  });

  it("says why a sign-in failed without revealing whether the user exists", async () => {
    vi.spyOn(api, "me").mockRejectedValue(new ApiError(401, "no", null));
    vi.spyOn(api, "login").mockRejectedValue(
      new ApiError(400, "Invalid username or password.", {}),
    );
    renderApp();

    await userEvent.type(await screen.findByLabelText(/Username/), "nobody");
    await userEvent.type(screen.getByLabelText(/Password/), "x");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByText(/do not match an active account/)).toBeInTheDocument();
  });
});

describe("navigation follows capabilities", () => {
  it("offers a student no administration", async () => {
    vi.spyOn(api, "me").mockResolvedValue(me());
    renderApp();
    const nav = await screen.findByRole("complementary", { name: "Main navigation" });
    expect(within(nav).getByRole("link", { name: /Documents/ })).toBeInTheDocument();
    expect(within(nav).queryByText("Administration")).toBeNull();
    expect(within(nav).queryByRole("link", { name: /Users/ })).toBeNull();
  });

  it("offers a department admin people and audit, not system status", async () => {
    vi.spyOn(api, "me").mockResolvedValue(
      me({
        role: "DEPARTMENT_ADMIN",
        capabilities: {
          ...NONE,
          administer_users: true,
          view_audit_log: true,
          view_operations: true,
        },
      }),
    );
    renderApp();
    const nav = await screen.findByRole("complementary", { name: "Main navigation" });
    expect(within(nav).getByRole("link", { name: /Users/ })).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: /Audit log/ })).toBeInTheDocument();
    expect(within(nav).queryByRole("link", { name: /System status/ })).toBeNull();
  });

  it("explains, rather than breaks, when a screen is typed in without access", async () => {
    vi.spyOn(api, "me").mockResolvedValue(me());
    renderApp("/admin/system");
    expect(await screen.findByText("You do not have access to this page")).toBeInTheDocument();
  });
});

const CITATION: Citation = {
  marker: 1,
  document_id: 7,
  document_title: "Academic Regulations",
  page_number: 2,
  chunk_id: 44,
  character_start: 10,
  character_end: 42,
  text: "minimum of 75 percent attendance",
  relevance: 0.9,
};

function chatResponse(overrides = {}) {
  return fromResponse({
    conversation_id: 3,
    question: "q",
    resolved_question: "",
    answer: "You need 75 percent attendance [1].",
    abstained: false,
    abstention_reason: "",
    citations: [CITATION],
    grounding: { support_ratio: 0.8, failures: [] },
    retrieval: {
      retrieved: 5,
      reranked: 5,
      passages_used: 2,
      semantic: true,
      reranker: "local-cross-encoder",
      suspected_injections: 0,
    },
    model: { name: "extractive-answerer", version: "0.1.0", generative: false },
    plan: { planned: false, steps: [], unanswered: [] },
    duration_ms: 420,
    timings: { retrieval: 100 },
    notes: [],
    message_id: 11,
    ...overrides,
  });
}

describe("answers", () => {
  it("turns each citation marker into a button that opens its source", async () => {
    const onInspect = vi.fn();
    render(
      <MemoryRouter>
        <ToastProvider>
          <Answer answer={chatResponse()} onInspect={onInspect} />
        </ToastProvider>
      </MemoryRouter>,
    );

    await userEvent.click(
      screen.getByRole("button", { name: /Source 1: Academic Regulations, page 2/ }),
    );
    expect(onInspect).toHaveBeenCalledWith(CITATION, "You need 75 percent attendance [1].");
    expect(screen.getByText("Quoted from sources")).toBeInTheDocument();
  });

  it("explains an abstention in plain language", () => {
    render(
      <MemoryRouter>
        <ToastProvider>
          <Answer
            answer={chatResponse({
              abstained: true,
              abstention_reason: "no authorized documents matched the question",
              citations: [],
              answer: "I couldn't find enough information in the provided documents.",
            })}
            onInspect={() => undefined}
          />
        </ToastProvider>
      </MemoryRouter>,
    );
    expect(screen.getByText("Not enough evidence to answer")).toBeInTheDocument();
    expect(screen.getByText(/do not have access to/)).toBeInTheDocument();
  });

  it("shows the exact evidence, highlighted, and warns when it is out of date", async () => {
    vi.spyOn(api, "source").mockResolvedValue({
      document_id: 7,
      document_title: "Academic Regulations",
      chunk_id: 44,
      page_number: 2,
      page_count: 3,
      character_start: 10,
      character_end: 42,
      chunk_text: "minimum of 75 percent attendance",
      page_text: "Students: minimum of 75 percent attendance is required.",
      extraction_method: "TEXT_LAYER",
      version_number: 1,
      is_current: false,
    });

    render(
      <MemoryRouter>
        <SourceDialog
          citation={CITATION}
          quote="minimum of 75 percent attendance [1]"
          onClose={() => undefined}
        />
      </MemoryRouter>,
    );

    // The words the answer quoted carry the strong highlight.
    const quoted = await screen.findByText("minimum of 75 percent attendance");
    expect(quoted.tagName).toBe("MARK");
    expect(quoted).toHaveClass("evidence");
    expect(screen.getByText(/document has changed since this answer/)).toBeInTheDocument();
    await waitFor(() => expect(api.source).toHaveBeenCalledWith(7, 44));
  });
});
