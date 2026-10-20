import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import type { Me, SearchHit, SearchResponse } from "../api/types";
import App from "../App";
import { AuthProvider } from "../auth/AuthContext";
import { ToastProvider } from "../components/ui";

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

const hit = (chunk: number, title: string, text: string, below_floor = false): SearchHit => ({
  document_id: chunk * 10,
  document_title: title,
  page_number: 1,
  chunk_id: chunk,
  character_start: 0,
  character_end: text.length,
  text,
  score: 0.03,
  relevance: below_floor ? 0.001 : 0.97,
  below_floor,
  matched_by: ["lexical", "vector"],
  lexical_rank: 1,
  vector_rank: 1,
});

const response = (results: SearchHit[], below_floor: number, extra: Partial<SearchResponse> = {}): SearchResponse => ({
  query: "late fine",
  count: results.length,
  limit: 20,
  max_limit: 50,
  semantic: true,
  embedding_model: "sentence-transformers BAAI/bge-small-en-v1.5",
  retrieval: { lexical_candidates: 3, vector_candidates: 3, authorized_documents: 3 },
  ranking: {
    reranker: "local-cross-encoder cross-encoder/ms-marco-MiniLM-L6-v2",
    degraded: false,
    relevance_floor: 0.03,
    reranked: results.length + below_floor,
    below_floor,
  },
  notes: [],
  results,
  ...extra,
});

const ANSWER = hit(1, "Library Rules", "The late return fine is 5 rupees per day per book.");
const FRAGMENT = hit(2, "Scanned fragment", "Fina| Resu|ts Dead|ine: I 2", true);

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

describe("SearchPage", () => {
  it("shows passages above the relevance floor and offers the rest on request", async () => {
    const search = vi
      .spyOn(api, "search")
      .mockResolvedValueOnce(response([ANSWER], 1))
      .mockResolvedValueOnce(response([ANSWER, FRAGMENT], 1));
    renderSearch();
    await searchFor("late fine");

    expect(await screen.findByRole("link", { name: "Library Rules" })).toBeInTheDocument();
    expect(screen.queryByText("Scanned fragment")).not.toBeInTheDocument();
    expect(screen.getByText("Ranked by relevance")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Show 1 weaker match" }));

    expect(search).toHaveBeenLastCalledWith(expect.objectContaining({ include_below_floor: true, query: "late fine" }));
    const weaker = await screen.findByRole("list", { name: "Weaker matches" });
    expect(weaker).toHaveTextContent("Scanned fragment");
    // The results list still holds only the passage above the floor. Found by
    // name: the first list on the page is the sidebar's navigation.
    const results = screen.getByRole("list", { name: "Search results" });
    expect(results).toHaveTextContent("Library Rules");
    expect(results).not.toHaveTextContent("Scanned fragment");
  });

  it("moves focus to the weaker matches when the button asking for them goes away", async () => {
    vi.spyOn(api, "search")
      .mockResolvedValueOnce(response([ANSWER], 1))
      .mockResolvedValueOnce(response([ANSWER, FRAGMENT], 1));
    renderSearch();
    await searchFor("late fine");

    await userEvent.click(await screen.findByRole("button", { name: "Show 1 weaker match" }));

    const heading = await screen.findByRole("heading", { name: "Weaker matches" });
    expect(screen.queryByRole("button", { name: "Show 1 weaker match" })).not.toBeInTheDocument();
    await waitFor(() => expect(heading).toHaveFocus());
  });

  it("offers more results only while the matches themselves filled the page", async () => {
    // Weaker matches asked for are appended beyond the limit, so the response
    // can hold `limit` passages while every match is already on screen.
    const three = [1, 2, 3].map((chunk) => hit(chunk, `Match ${chunk}`, "late fine"));
    const weaker = Array.from({ length: 9 }, (_, index) => hit(10 + index, `Weak ${index}`, "fine", true));
    vi.spyOn(api, "search")
      .mockResolvedValueOnce(response(three, 9, { limit: 10 }))
      .mockResolvedValueOnce(response([...three, ...weaker], 9, { limit: 10 }));
    renderSearch();
    await searchFor("late fine");

    await userEvent.click(await screen.findByRole("button", { name: "Show 9 weaker matches" }));

    expect(await screen.findByRole("list", { name: "Weaker matches" })).toHaveTextContent("Weak 8");
    expect(screen.queryByRole("button", { name: "Show more results" })).not.toBeInTheDocument();
  });

  it("says there is no close match and shows the closest only as weaker matches", async () => {
    vi.spyOn(api, "search").mockResolvedValue(response([FRAGMENT], 4));
    renderSearch();
    await searchFor("quantum chromodynamics");

    expect(await screen.findByText("No close match")).toBeInTheDocument();
    expect(screen.getByText(/4 passages share words or meaning/)).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Weaker matches" })).toHaveTextContent("Scanned fragment");
    expect(screen.getByRole("button", { name: "Show all 4 weaker matches" })).toBeInTheDocument();
    // Weaker matches are not counted as results.
    expect(screen.getByText(/passages? from/)).toHaveTextContent("0 passages from 0 documents");
    expect(screen.queryByText("Nothing found")).not.toBeInTheDocument();
  });

  it("offers the weaker matches when the server returned none of them", async () => {
    vi.spyOn(api, "search").mockResolvedValue(response([], 4));
    renderSearch();
    await searchFor("quantum chromodynamics");

    expect(await screen.findByText("No close match")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Show the weaker matches" })).toBeInTheDocument();
  });

  it("still says nothing was found when retrieval found nothing at all", async () => {
    vi.spyOn(api, "search").mockResolvedValue(response([], 0));
    renderSearch();
    await searchFor("zzz");

    expect(await screen.findByText("Nothing found")).toBeInTheDocument();
  });

  it("lets the server choose the page size, and pages by it up to the server's maximum", async () => {
    const ten = Array.from({ length: 10 }, (_, index) => hit(index + 1, `Doc ${index + 1}`, "late fine"));
    const search = vi
      .spyOn(api, "search")
      .mockResolvedValueOnce(response(ten, 0, { limit: 10, max_limit: 15 }))
      .mockResolvedValueOnce(response(ten, 0, { limit: 15, max_limit: 15 }));
    renderSearch();
    await searchFor("late fine");

    expect(await screen.findByRole("link", { name: "Doc 1" })).toBeInTheDocument();
    // A new search sends no limit: a server configured with a smaller
    // maximum than any client-side page size would reject it outright.
    expect(search.mock.calls[0][0]).not.toHaveProperty("limit");

    await userEvent.click(screen.getByRole("button", { name: "Show more results" }));
    expect(search).toHaveBeenLastCalledWith(expect.objectContaining({ limit: 15 }));
  });

  it("asks for more results only up to the server's maximum", async () => {
    const full = Array.from({ length: 20 }, (_, index) => hit(index + 1, `Doc ${index + 1}`, "late fine"));
    vi.spyOn(api, "search").mockResolvedValue(response(full, 0, { max_limit: 20 }));
    renderSearch();
    await searchFor("late fine");

    expect(await screen.findByRole("link", { name: "Doc 1" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Show more results" })).not.toBeInTheDocument();
  });
});
