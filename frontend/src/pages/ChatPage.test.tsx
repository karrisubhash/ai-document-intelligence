import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { tagRequest } from "../api/cache";
import { ApiError, api } from "../api/client";
import type {
  ChatMessage,
  ChatResponse,
  Collection,
  Conversation,
  Me,
  SearchResponse,
} from "../api/types";
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

const EXAMS: Conversation = {
  id: 8,
  title: "Exam dates",
  department: "Physics",
  created_at: "2026-10-05T10:00:00Z",
  updated_at: "2026-10-05T10:00:00Z",
  messages: [
    message(5, "USER", "When do exams start?"),
    message(6, "ASSISTANT", "Exams start on 3 March."),
  ],
};

function summaryOf(conversation: Conversation) {
  return {
    id: conversation.id,
    title: conversation.title,
    department: conversation.department,
    created_at: conversation.created_at,
    updated_at: conversation.updated_at,
    message_count: conversation.messages.length,
  };
}

const BOTH = { count: 2, next: null, previous: null, results: [summaryOf(HOSTEL), summaryOf(EXAMS)] };

function abstained(conversationId: number): ChatResponse {
  return {
    ...answer(conversationId, "Not enough evidence to answer."),
    abstained: true,
    abstention_reason: "no_relevant_passages",
  };
}

function LocationProbe() {
  const location = useLocation();
  return (
    <>
      <output data-testid="location">{location.pathname}</output>
      <output data-testid="query-string">{location.search}</output>
    </>
  );
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

describe("ChatPage Search instead", () => {
  const QUESTION = "What is the parking fee for visiting parents?";
  const NOTHING_FOUND: SearchResponse = {
    query: QUESTION,
    count: 0,
    limit: 20,
    max_limit: 50,
    semantic: true,
    embedding_model: "",
    retrieval: { lexical_candidates: 0, vector_candidates: 0, authorized_documents: 0 },
    ranking: { reranker: "", degraded: false, relevance_floor: null, reranked: 0, below_floor: 0 },
    notes: [],
    results: [],
  };

  it("hands the question to Search without putting it in the address", async () => {
    vi.spyOn(api, "chatStream").mockResolvedValue(abstained(3));
    const search = vi.spyOn(api, "search").mockResolvedValue(NOTHING_FOUND);
    renderAt("/ask");
    await userEvent.type(await screen.findByLabelText("Your question"), `${QUESTION}{Enter}`);
    await userEvent.click(await screen.findByRole("link", { name: "Search instead" }));

    // Search has the question, and runs it...
    expect(await screen.findByLabelText("Search documents")).toHaveValue(QUESTION);
    await waitFor(() =>
      expect(search).toHaveBeenCalledWith(expect.objectContaining({ query: QUESTION })),
    );
    // ...and the address has not: what people ask stays out of browser
    // history, and out of the access log a reload would write.
    expect(screen.getByTestId("location")).toHaveTextContent(/^\/search$/);
    expect(screen.getByTestId("query-string")).toBeEmptyDOMElement();
  });

  it("searches within the collection the question was asked in", async () => {
    const examPack = { id: 5, name: "Exam pack" } as unknown as Collection;
    vi.spyOn(api, "collections").mockResolvedValue({
      count: 1,
      next: null,
      previous: null,
      results: [examPack],
    });
    vi.spyOn(api, "chatStream").mockResolvedValue(abstained(3));
    const search = vi.spyOn(api, "search").mockResolvedValue(NOTHING_FOUND);
    renderAt("/ask");
    await userEvent.selectOptions(await screen.findByRole("combobox", { name: "Ask within" }), "5");
    await userEvent.type(screen.getByLabelText("Your question"), `${QUESTION}{Enter}`);
    await userEvent.click(await screen.findByRole("link", { name: "Search instead" }));

    await waitFor(() =>
      expect(search).toHaveBeenCalledWith(
        expect.objectContaining({ query: QUESTION, collection_id: 5 }),
      ),
    );
  });
});

describe("ChatPage edits", () => {
  it("does not carry an edit into another conversation", async () => {
    vi.spyOn(api, "conversations").mockResolvedValue(BOTH);
    vi.spyOn(api, "conversation").mockImplementation(async (id) => (id === 8 ? EXAMS : HOSTEL));
    const stream = vi.spyOn(api, "chatStream").mockResolvedValue(answer(8, "On the notice board."));
    renderAt("/ask/7");
    await userEvent.click(await screen.findByRole("button", { name: "Edit question" }));
    expect(screen.getByText(/Editing your last question/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("link", { name: /Exam dates/ }));
    expect(await screen.findByText("When do exams start?")).toBeInTheDocument();
    expect(screen.queryByText(/Editing your last question/)).not.toBeInTheDocument();
    const box = screen.getByLabelText("Your question");
    expect(box).toHaveValue("");

    // A new question here - not the other conversation's answer to
    // replace, which the server refuses.
    await userEvent.type(box, "And the timetable?{Enter}");
    const [input] = stream.mock.calls[0];
    expect(input).toEqual(
      expect.objectContaining({ question: "And the timetable?", conversation_id: 8 }),
    );
    expect(input).not.toHaveProperty("replaces");
  });

  it("does not carry an edit to a new question", async () => {
    renderAt("/ask/7");
    await userEvent.click(await screen.findByRole("button", { name: "Edit question" }));
    await userEvent.click(screen.getByRole("button", { name: "New question" }));

    expect(await screen.findByText("Ask a question about your documents")).toBeInTheDocument();
    expect(screen.queryByText(/Editing your last question/)).not.toBeInTheDocument();
    expect(screen.getByLabelText("Your question")).toHaveValue("");
  });
});

describe("ChatPage reopening a remembered conversation", () => {
  // Conversation 7, then 8, then 7 again: the reopening shows what the
  // first visit loaded at once (api/cache.ts), while its own load - which
  // the test answers - runs.
  let answerReopening: (conversation: Conversation) => void = () => undefined;
  let failReopening: (error: unknown) => void = () => undefined;

  async function reopen() {
    let reopened = false;
    vi.spyOn(api, "conversations").mockResolvedValue(BOTH);
    vi.spyOn(api, "conversation").mockImplementation((id) => {
      const path = `/api/v1/chat/conversations/${id}/`;
      if (id === 7 && reopened) {
        return tagRequest(
          new Promise<Conversation>((resolve, reject) => {
            answerReopening = resolve;
            failReopening = reject;
          }),
          path,
        );
      }
      return tagRequest(Promise.resolve(id === 8 ? EXAMS : HOSTEL), path);
    });
    renderAt("/ask/7");
    await screen.findByText("What is the hostel fee?");
    await userEvent.click(screen.getByRole("link", { name: /Exam dates/ }));
    await screen.findByText("When do exams start?");

    reopened = true;
    await userEvent.click(screen.getByRole("link", { name: /Hostel fees/ }));
    expect(await screen.findByText("What is the hostel fee?")).toBeInTheDocument();
  }

  it("shows a turn asked elsewhere once its own load arrives", async () => {
    await reopen();
    answerReopening({
      ...HOSTEL,
      messages: [
        ...HOSTEL.messages,
        message(3, "USER", "Is there a mess fee?"),
        message(4, "ASSISTANT", "The mess fee is 30000."),
      ],
    });

    expect(await screen.findByText("Is there a mess fee?")).toBeInTheDocument();
  });

  it("says the conversation could not be opened once its load fails", async () => {
    await reopen();
    failReopening(new ApiError(404, "Not found.", { detail: "Not found." }));

    expect(await screen.findByText("This conversation could not be opened")).toBeInTheDocument();
    expect(screen.queryByText("What is the hostel fee?")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Your question")).toBeDisabled();
  });
});

describe("ChatPage bulk delete", { timeout: 15000 }, () => {
  // The first query waits for the app shell and the sidebar to load.
  const LOADED = { timeout: 5000 };
  const summary = (id: number, title: string) => ({
    id,
    title,
    department: "Physics",
    created_at: HOSTEL.created_at,
    updated_at: HOSTEL.updated_at,
    message_count: 2,
  });
  const THREE = [summary(7, "Hostel fees"), summary(8, "Exam dates"), summary(9, "Library hours")];

  it("deletes the selected conversations and leaves one that was open", async () => {
    vi.spyOn(api, "conversations").mockResolvedValue({
      count: 3,
      next: null,
      previous: null,
      results: THREE,
    });
    const remove = vi
      .spyOn(api, "bulkDeleteConversations")
      .mockResolvedValue({ deleted: [7, 8], not_found: [] });
    renderAt("/ask/7");

    await userEvent.click(
      await screen.findByRole("button", { name: "Select conversations" }, LOADED),
    );
    await userEvent.click(screen.getByRole("checkbox", { name: "Select Hostel fees" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Select Exam dates" }));
    expect(screen.getByText("2 selected")).toBeInTheDocument();
    // Rename and delete-one are out of the way while selecting.
    expect(screen.queryByRole("button", { name: "Delete Hostel fees" })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Delete selected" }));
    const dialog = screen.getByRole("dialog", { name: "Delete 2 conversations?" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete" }));

    expect(remove).toHaveBeenCalledWith({ ids: [7, 8] });
    expect(await screen.findByText("Deleted 2 conversations.")).toBeInTheDocument();
    // The open conversation was among them, so the page leaves it. Router
    // navigations commit in a transition, after the toast's urgent update.
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(/^\/ask$/));
  });

  it("deletes every conversation, including ones not loaded yet", async () => {
    vi.spyOn(api, "conversations").mockResolvedValue({
      count: 120,
      next: "?page=2",
      previous: null,
      results: THREE,
    });
    const remove = vi
      .spyOn(api, "bulkDeleteConversations")
      .mockResolvedValueOnce({
        deleted: Array.from({ length: 100 }, (_, n) => n + 1),
        not_found: [],
        remaining: 20,
        as_of: "2026-10-20T09:00:00Z",
      })
      .mockResolvedValueOnce({
        deleted: Array.from({ length: 20 }, (_, n) => n + 101),
        not_found: [],
        remaining: 0,
        as_of: "2026-10-20T09:00:00Z",
      });
    renderAt("/ask");

    await userEvent.click(
      await screen.findByRole("button", { name: "Select conversations" }, LOADED),
    );
    await userEvent.click(screen.getByRole("checkbox", { name: "Select all conversations" }));
    expect(screen.getByText("All 120 selected")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Delete selected" }));
    const dialog = screen.getByRole("dialog", { name: "Delete all 120 conversations?" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete all" }));

    expect(remove).toHaveBeenCalledTimes(2);
    // The second round keeps to the first answer's snapshot.
    expect(remove).toHaveBeenLastCalledWith({ all: true, as_of: "2026-10-20T09:00:00Z" });
    expect(await screen.findByText("Deleted 120 conversations.")).toBeInTheDocument();
  });

  it("deletes exactly the conversations shown when all of them are loaded", async () => {
    vi.spyOn(api, "conversations").mockResolvedValue({
      count: 3,
      next: null,
      previous: null,
      results: THREE,
    });
    const remove = vi
      .spyOn(api, "bulkDeleteConversations")
      .mockResolvedValue({ deleted: [7, 8, 9], not_found: [] });
    renderAt("/ask");

    await userEvent.click(
      await screen.findByRole("button", { name: "Select conversations" }, LOADED),
    );
    await userEvent.click(screen.getByRole("checkbox", { name: "Select all conversations" }));
    await userEvent.click(screen.getByRole("button", { name: "Delete selected" }));
    const dialog = screen.getByRole("dialog", { name: "Delete all 3 conversations?" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete all" }));

    // Ids, not all: true - a conversation started in another tab since is
    // not on screen, so it is not deleted.
    expect(remove).toHaveBeenCalledWith({ ids: [7, 8, 9] });
  });

  it("keeps the dialog open with the error, and cannot be cancelled mid-run", async () => {
    vi.spyOn(api, "conversations").mockResolvedValue({
      count: 3,
      next: null,
      previous: null,
      results: THREE,
    });
    let fail: (error: Error) => void = () => undefined;
    vi.spyOn(api, "bulkDeleteConversations").mockImplementation(
      () => new Promise((_, reject) => (fail = reject)),
    );
    renderAt("/ask");

    await userEvent.click(
      await screen.findByRole("button", { name: "Select conversations" }, LOADED),
    );
    await userEvent.click(screen.getByRole("checkbox", { name: "Select Exam dates" }));
    await userEvent.click(screen.getByRole("button", { name: "Delete selected" }));
    const dialog = screen.getByRole("dialog", { name: "Delete 1 conversation?" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete" }));

    // While it runs, Cancel is disabled and Escape does not close it.
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeDisabled();
    await userEvent.keyboard("{Escape}");
    expect(screen.getByRole("dialog", { name: "Delete 1 conversation?" })).toBeInTheDocument();

    fail(new Error("Server unavailable"));
    expect(await within(dialog).findByText("That did not work")).toBeInTheDocument();
    expect(screen.getByText("1 selected")).toBeInTheDocument();
  });

  it("narrows 'all' back to the loaded ones when one is unticked", async () => {
    vi.spyOn(api, "conversations").mockResolvedValue({
      count: 120,
      next: "?page=2",
      previous: null,
      results: THREE,
    });
    const remove = vi
      .spyOn(api, "bulkDeleteConversations")
      .mockResolvedValue({ deleted: [8, 9], not_found: [] });
    renderAt("/ask");

    await userEvent.click(
      await screen.findByRole("button", { name: "Select conversations" }, LOADED),
    );
    await userEvent.click(screen.getByRole("checkbox", { name: "Select all conversations" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Select Hostel fees" }));
    expect(screen.getByText("2 selected")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Delete selected" }));
    const dialog = screen.getByRole("dialog", { name: "Delete 2 conversations?" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete" }));

    expect(remove).toHaveBeenCalledWith({ ids: [8, 9] });
  });
});
