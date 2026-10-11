import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import type { ChatMessage, ChatResponse, Conversation, Me } from "../api/types";
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
    create_collections: false,
  },
};

function message(id: number, role: ChatMessage["role"], content: string): ChatMessage {
  return {
    id,
    role,
    content,
    abstained: false,
    abstention_reason: "",
    citations: [],
    support_ratio: 1,
    model_name: "extractive-answerer",
    model_version: "0.1.0",
    generative: false,
    retrieved: 1,
    passages_used: 1,
    duration_ms: 10,
    metadata: {},
    created_at: "2026-10-05T10:00:00Z",
  };
}

const HOSTEL: Conversation = {
  id: 7,
  title: "Hostel fees",
  department: "Physics",
  created_at: "2026-10-05T10:00:00Z",
  updated_at: "2026-10-05T10:00:00Z",
  messages: [
    message(1, "USER", "What is the hostel fee?"),
    message(2, "ASSISTANT", "The hostel fee is 60000 [1]."),
  ],
};

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
  return <output data-testid="location">{useLocation().pathname}</output>;
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

beforeEach(() => {
  vi.spyOn(api, "me").mockResolvedValue(ME);
  vi.spyOn(api, "departments").mockResolvedValue([]);
  vi.spyOn(api, "conversations").mockResolvedValue({
    count: 1,
    next: null,
    previous: null,
    results: [
      {
        id: 7,
        title: "Hostel fees",
        department: "Physics",
        created_at: HOSTEL.created_at,
        updated_at: HOSTEL.updated_at,
        message_count: 2,
      },
    ],
  });
  vi.spyOn(api, "conversation").mockResolvedValue(HOSTEL);
});

afterEach(() => vi.restoreAllMocks());

describe("ChatPage", () => {
  it("does not let a late first answer take over the conversation the user moved to", async () => {
    let deliver: (response: ChatResponse) => void = () => undefined;
    vi.spyOn(api, "chatStream").mockImplementation(
      () => new Promise<ChatResponse>((resolve) => (deliver = resolve)),
    );

    renderAt("/ask");
    await userEvent.type(await screen.findByLabelText("Your question"), "Pending question{Enter}");
    expect(await screen.findByText("Pending question")).toBeInTheDocument();

    // While it is being answered, open another conversation.
    await userEvent.click(screen.getByRole("link", { name: /Hostel fees/ }));
    expect(await screen.findByText("What is the hostel fee?")).toBeInTheDocument();
    // Its turns replace the new-question screen's at once.
    expect(screen.queryByText("Pending question")).not.toBeInTheDocument();

    deliver(answer(42, "The late answer."));
    await waitFor(() => expect(api.conversations).toHaveBeenCalledTimes(2));

    // Still the conversation the user opened - not /ask/42 with conversation
    // 7's turns on screen and the next question sent to 42.
    expect(screen.getByTestId("location")).toHaveTextContent("/ask/7");
    expect(screen.getByText("What is the hostel fee?")).toBeInTheDocument();
    expect(screen.queryByText("The late answer.")).not.toBeInTheDocument();
  });

  it("shows nothing of the previous conversation while the next one loads", async () => {
    let open: (conversation: Conversation) => void = () => undefined;
    vi.spyOn(api, "chatStream").mockResolvedValue(answer(3, "First answer."));
    vi.spyOn(api, "conversation").mockImplementation(
      () => new Promise<Conversation>((resolve) => (open = resolve)),
    );

    renderAt("/ask");
    await userEvent.type(await screen.findByLabelText("Your question"), "Opening question{Enter}");
    expect(await screen.findByText("First answer.")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("link", { name: /Hostel fees/ }));

    expect(screen.queryByText("First answer.")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Your question")).toBeDisabled();

    open(HOSTEL);
    expect(await screen.findByText("What is the hostel fee?")).toBeInTheDocument();
    expect(screen.getByLabelText("Your question")).toBeEnabled();
  });
});
