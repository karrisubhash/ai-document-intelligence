import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, api } from "../api/client";
import type { Collection, Me, SearchHit, SearchResponse } from "../api/types";
import App from "../App";
import { AuthProvider } from "../auth/AuthContext";
import { ToastProvider } from "../components/ui";
import { passageSegments, unwrapLines, unwrapSegments } from "../lib/highlight";

const STUDENT: Me = {
  id: 1,
  username: "asha",
  email: "",
  first_name: "Asha",
  last_name: "Rao",
  is_superuser: false,
  role: "STUDENT",
  department: "Computer Science",
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

const PASSAGE = "Every contract follows the regulation on fees.\nThe act of signing is binding.";

const HIT: SearchHit = {
  document_id: 10,
  document_title: "Fee Rules",
  page_number: 4,
  chunk_id: 1,
  character_start: 100,
  character_end: 100 + PASSAGE.length,
  text: PASSAGE,
  score: 0.03,
  relevance: 0.97,
  below_floor: false,
  matched_by: ["lexical"],
  lexical_rank: 1,
  vector_rank: null,
};

function response(extra: Partial<SearchResponse> = {}): SearchResponse {
  return {
    query: "regulations act",
    count: 1,
    limit: 10,
    max_limit: 50,
    semantic: true,
    embedding_model: "sentence-transformers BAAI/bge-small-en-v1.5",
    retrieval: { lexical_candidates: 1, vector_candidates: 1, authorized_documents: 3 },
    ranking: {
      reranker: "local-cross-encoder cross-encoder/ms-marco-MiniLM-L6-v2",
      degraded: false,
      relevance_floor: 0.03,
      reranked: 1,
      below_floor: 0,
    },
    notes: [],
    notices: [],
    results: [HIT],
    groups: [
      {
        document_id: 10,
        document_title: "Fee Rules",
        document_type: "POLICY",
        department: "Accounts",
        updated_at: new Date(Date.now() - 3 * 86_400_000).toISOString(),
        file_name: "fees.pdf",
        file_kind: "pdf",
        version_number: 2,
        relevance: 0.97,
        matched_by: ["lexical"],
        passages: [
          {
            chunk_ids: [1, 2],
            page_number: 4,
            character_start: 100,
            character_end: 100 + PASSAGE.length,
            text: PASSAGE,
            // "regulation" and "act" as the server matched them - not the
            // "act" inside "contract".
            highlights: [
              { start: 27, end: 37 },
              { start: 51, end: 54 },
            ],
            snippet: { start: 0, end: PASSAGE.length },
            relevance: 0.97,
            matched_by: ["lexical"],
            chunk_kind: "",
          },
        ],
        more_passages: 2,
      },
    ],
    server_ms: 420,
    ...extra,
  };
}

/** One document's results, a page of one that has room for more. */
function titled(title: string, documentId: number): SearchResponse {
  const base = response();
  return response({
    limit: 1,
    results: [{ ...HIT, document_id: documentId, document_title: title }],
    groups: base.groups!.map((group) => ({ ...group, document_id: documentId, document_title: title })),
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

const COLLECTION = {
  id: 5,
  name: "Exam pack",
  description: "",
  department: "Computer Science",
  created_by: 1,
  created_by_username: "asha",
  documents: [],
  document_count: 0,
  can_manage: false,
  created_at: "2026-10-01T00:00:00Z",
  updated_at: "2026-10-01T00:00:00Z",
} as unknown as Collection;

function renderSearch() {
  render(
    <MemoryRouter initialEntries={["/search"]}>
      <AuthProvider>
        <ToastProvider>
          <App />
        </ToastProvider>
      </AuthProvider>
    </MemoryRouter>,
  );
}

async function searchFor(text: string) {
  await userEvent.type(await screen.findByLabelText("Search documents"), text);
  await userEvent.click(screen.getByRole("button", { name: "Search" }));
}

beforeEach(() => {
  vi.spyOn(api, "me").mockResolvedValue(STUDENT);
  vi.spyOn(api, "departments").mockResolvedValue([]);
  vi.spyOn(api, "collections").mockResolvedValue({ count: 0, next: null, previous: null, results: [] });
});

afterEach(() => vi.restoreAllMocks());

describe("search results by document", () => {
  it("shows one card per document with the server's highlights and facts", async () => {
    vi.spyOn(api, "search").mockResolvedValue(response());
    renderSearch();
    await searchFor("regulations act");

    const title = await screen.findByRole("link", { name: "Fee Rules" });
    expect(title).toHaveAttribute("href", `/documents/10?tab=text&page=4&start=100&end=${100 + PASSAGE.length}`);
    const card = title.closest(".result") as HTMLElement;
    expect([...card.querySelectorAll("mark")].map((mark) => mark.textContent)).toEqual(["regulation", "act"]);
    expect(card).toHaveTextContent("Policy");
    expect(card).toHaveTextContent("Accounts");
    expect(card).toHaveTextContent("Updated 3 days ago");
    expect(card).toHaveTextContent("Strong match");
    expect(card).toHaveTextContent("2 more passages in this document");
    expect(screen.getByRole("link", { name: "Page 4 of Fee Rules" })).toBeInTheDocument();
    expect(screen.getByText(/0\.4 s/)).toBeInTheDocument();
  });

  it("asks the server to group by document", async () => {
    const search = vi.spyOn(api, "search").mockResolvedValue(response());
    renderSearch();
    await searchFor("fees");

    await screen.findByRole("link", { name: "Fee Rules" });
    expect(search).toHaveBeenLastCalledWith(expect.objectContaining({ group_by: "document" }));
  });

  it("groups by itself when the server sent no groups", async () => {
    const second = { ...HIT, chunk_id: 2, page_number: 7, highlights: [], snippet: { start: 0, end: 10 } };
    vi.spyOn(api, "search").mockResolvedValue(response({ groups: undefined, results: [HIT, second] }));
    renderSearch();
    await searchFor("fees");

    const title = await screen.findByRole("link", { name: "Fee Rules" });
    const card = title.closest(".result") as HTMLElement;
    expect(card.querySelectorAll(".result-passage")).toHaveLength(2);
    expect(screen.getAllByRole("link", { name: "Fee Rules" })).toHaveLength(1);
  });

  it("puts a notice in plain words and keeps the server's diagnostics from students", async () => {
    vi.spyOn(api, "search").mockResolvedValue(
      response({
        results: [],
        groups: [],
        notices: [{ code: "FILTERS_EXCLUDE_ALL", params: {} }],
        notes: ["no authorized documents match the filters"],
      }),
    );
    renderSearch();
    await searchFor("fees");

    expect(await screen.findByText("Your filters leave nothing to search")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
    expect(screen.queryByText("no authorized documents match the filters")).not.toBeInTheDocument();
    expect(screen.queryByText("Technical details")).not.toBeInTheDocument();
    expect(screen.queryByText("Nothing found")).not.toBeInTheDocument();
  });

  it("shows system administrators the diagnostics behind a disclosure", async () => {
    vi.spyOn(api, "me").mockResolvedValue({
      ...STUDENT,
      role: "SYSTEM_ADMIN",
      capabilities: { ...STUDENT.capabilities, view_system_status: true, view_operations: true },
    });
    vi.spyOn(api, "search").mockResolvedValue(
      response({
        results: [],
        groups: [],
        notices: [{ code: "STALE_INDEX", params: { documents: 4 } }],
        notes: ["4 authorized document(s) with 9 chunk(s), none of which belongs to the current pass"],
      }),
    );
    renderSearch();
    await searchFor("fees");

    expect(await screen.findByText("Search is out of step with these documents")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open Processing" })).toBeInTheDocument();
    expect(screen.getByText("Technical details")).toBeInTheDocument();
    expect(screen.getByText(/none of which belongs/)).toBeInTheDocument();
  });

  it("preselects a collection handed off from its own Search button, without running a search", async () => {
    vi.spyOn(api, "collections").mockResolvedValue({ count: 1, next: null, previous: null, results: [COLLECTION] });
    const search = vi.spyOn(api, "search");
    render(
      <MemoryRouter initialEntries={[{ pathname: "/search", state: { collectionId: 5 } }]}>
        <AuthProvider>
          <ToastProvider>
            <App />
          </ToastProvider>
        </AuthProvider>
      </MemoryRouter>,
    );

    const select = await screen.findByRole("combobox", { name: "Collection" });
    await waitFor(() => expect(select).toHaveValue("5"));
    expect(search).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /Remove filter Collection: Exam pack/ })).toBeInTheDocument();
  });

  it("applies a filter at once, shows it as a chip, and removes it", async () => {
    const search = vi.spyOn(api, "search").mockResolvedValue(response());
    renderSearch();
    await searchFor("fees");
    await screen.findByRole("link", { name: "Fee Rules" });

    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Type" }), "POLICY");

    await waitFor(() =>
      expect(search).toHaveBeenLastCalledWith(expect.objectContaining({ query: "fees", document_type: "POLICY" })),
    );
    await userEvent.click(screen.getByRole("button", { name: "Remove filter Type: Policy" }));
    await waitFor(() =>
      expect(search).toHaveBeenLastCalledWith(expect.objectContaining({ query: "fees", document_type: "" })),
    );
    expect(screen.queryByRole("button", { name: /Remove filter/ })).not.toBeInTheDocument();
  });

  it("shows the latest filters' results when an older, slower search answers last", async () => {
    vi.spyOn(api, "collections").mockResolvedValue({ count: 1, next: null, previous: null, results: [COLLECTION] });
    const typeOnly = deferred<SearchResponse>();
    const typeAndCollection = deferred<SearchResponse>();
    const search = vi
      .spyOn(api, "search")
      .mockResolvedValueOnce(response())
      .mockReturnValueOnce(typeOnly.promise)
      .mockReturnValueOnce(typeAndCollection.promise)
      .mockResolvedValue(titled("In the collection", 11));
    renderSearch();
    await searchFor("fees");
    await screen.findByRole("link", { name: "Fee Rules" });

    // A reranked search can take longer than the one after it.
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Type" }), "POLICY");
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Collection" }), "5");
    await act(async () => typeAndCollection.resolve(titled("In the collection", 11)));
    expect(await screen.findByRole("link", { name: "In the collection" })).toBeInTheDocument();
    await act(async () => typeOnly.resolve(titled("Any policy", 12)));

    expect(screen.getByRole("link", { name: "In the collection" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Any policy" })).not.toBeInTheDocument();
    // More of the search on screen - the one the controls show.
    await userEvent.click(screen.getByRole("button", { name: "Show more results" }));
    expect(search).toHaveBeenLastCalledWith(
      expect.objectContaining({ query: "fees", document_type: "POLICY", collection_id: 5, limit: 2 }),
    );
  });

  it("stays busy, and says nothing of an older search's failure, until the latest answers", async () => {
    vi.spyOn(api, "collections").mockResolvedValue({ count: 1, next: null, previous: null, results: [COLLECTION] });
    const typeOnly = deferred<SearchResponse>();
    const typeAndCollection = deferred<SearchResponse>();
    vi.spyOn(api, "search")
      .mockResolvedValueOnce(response())
      .mockReturnValueOnce(typeOnly.promise)
      .mockReturnValueOnce(typeAndCollection.promise);
    renderSearch();
    await searchFor("fees");
    await screen.findByRole("link", { name: "Fee Rules" });

    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Type" }), "POLICY");
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Collection" }), "5");
    await act(async () => typeOnly.reject(new ApiError(503, "The server had a problem handling this request.", null)));

    expect(document.querySelector(".search-results")).toHaveAttribute("aria-busy", "true");
    expect(screen.getByRole("button", { name: "Search" })).toBeDisabled();
    expect(screen.queryByText("Search failed")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Fee Rules" })).toBeInTheDocument();

    await act(async () => typeAndCollection.resolve(titled("In the collection", 11)));
    expect(await screen.findByRole("link", { name: "In the collection" })).toBeInTheDocument();
    expect(document.querySelector(".search-results")).toHaveAttribute("aria-busy", "false");
  });

  it("offers no department filter to someone who reads one department", async () => {
    renderSearch();
    await screen.findByLabelText("Search documents");

    expect(screen.queryByRole("combobox", { name: "Department" })).not.toBeInTheDocument();
    expect(api.departments).not.toHaveBeenCalled();
  });

  it("hands the question over with the scope it was searched in", async () => {
    vi.spyOn(api, "search").mockResolvedValue(response());
    vi.spyOn(api, "collections").mockResolvedValue({ count: 1, next: null, previous: null, results: [COLLECTION] });
    vi.spyOn(api, "conversations").mockResolvedValue({ count: 0, next: null, previous: null, results: [] });
    const chat = vi.spyOn(api, "chatStream").mockReturnValue(new Promise(() => undefined));
    renderSearch();
    await userEvent.selectOptions(await screen.findByRole("combobox", { name: "Collection" }), "5");
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Type" }), "POLICY");
    await searchFor("fees");
    await screen.findByRole("link", { name: "Fee Rules" });

    await userEvent.click(screen.getByRole("button", { name: "Ask this as a question instead" }));

    await waitFor(() =>
      expect(chat).toHaveBeenCalledWith(
        expect.objectContaining({ question: "fees", collection_id: 5, document_type: "POLICY" }),
        expect.anything(),
        // Ask passes an AbortSignal so Stop can end the answer.
        expect.anything(),
      ),
    );
    expect(await screen.findByText("Policy")).toBeInTheDocument();
  });
});

describe("unwrapSegments", () => {
  it("unwraps exactly as unwrapLines does, carrying the highlights along", () => {
    const texts = [
      "Every student shall maintain a\nminimum of 75 percent attendance in\n(a) theory and\n(b) laboratory",
      "the end-\nsemester examination",
      "counted\r\n  separately for theory",
      "Heading\nNext line.",
      "col one   col two\nrow two",
      "trailing\n",
    ];
    for (const text of texts) {
      expect(unwrapSegments(text, []).map((segment) => segment.text).join("")).toBe(unwrapLines(text));
    }
    const text = "a minimum of\n75 percent attendance";
    const segments = unwrapSegments(text, [
      [2, 9],
      [13, 15],
    ]);
    expect(segments.map((segment) => segment.text).join("")).toBe("a minimum of 75 percent attendance");
    expect(segments.filter((segment) => segment.highlighted).map((segment) => segment.text)).toEqual([
      "minimum",
      "75",
    ]);
  });

  it("windows a passage by code points and says what was cut", () => {
    const text = "😀 The fine is five rupees per day for every book.";
    // In code points the emoji is one character, so "fine" starts at 6.
    const view = passageSegments(text, [{ start: 6, end: 10 }], { start: 2, end: 30 });
    expect(view.clippedStart).toBe(true);
    expect(view.clippedEnd).toBe(true);
    expect(view.segments.find((segment) => segment.highlighted)?.text).toBe("fine");
  });
});
