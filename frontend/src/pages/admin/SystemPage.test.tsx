import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { aiApi } from "../../api/ai";
import { api } from "../../api/client";
import type { AILink, AIStatus, Me, SystemStatus } from "../../api/types";
import App from "../../App";
import { AuthProvider } from "../../auth/AuthContext";
import { ToastProvider } from "../../components/ui";
import { summary } from "./AIProvidersCard";

const ROOT: Me = {
  id: 1,
  username: "root",
  email: "",
  first_name: "Ada",
  last_name: "Root",
  is_superuser: true,
  role: "SYSTEM_ADMIN",
  department: "",
  capabilities: {
    administer_users: true,
    administer_departments: true,
    view_audit_log: true,
    view_operations: true,
    view_system_status: true,
    upload_to_any_department: true,
    create_collections: true,
  },
};

function link(overrides: Partial<AILink>): AILink {
  return {
    id: "gemini:gemini-3.5-flash",
    provider: "gemini",
    provider_display: "Gemini",
    model: "gemini-3.5-flash",
    label: "Gemini 3.5 Flash",
    state: "ready",
    cooling_until: null,
    calls_today: 3,
    daily_limit: 20,
    resets_at: "2026-11-08T08:00:00+00:00",
    last_error_type: null,
    last_error_status: null,
    last_error_at: null,
    last_success_at: "2026-11-07T11:58:00+00:00",
    trains_on_input: true,
    key_env: "GEMINI_API_KEY",
    ...overrides,
  };
}

const CHAIN = [
  link({}),
  link({
    id: "gemini:gemini-3-flash-preview",
    model: "gemini-3-flash-preview",
    label: "Gemini 3 Flash (preview)",
    state: "cooling",
    cooling_until: "2026-11-07T12:05:00+00:00",
    last_error_type: "RateLimitError",
    last_error_status: 429,
    last_error_at: "2026-11-07T12:04:00+00:00",
  }),
  link({
    id: "groq:openai/gpt-oss-120b",
    provider: "groq",
    provider_display: "Groq",
    model: "openai/gpt-oss-120b",
    label: "GPT-OSS 120B · Groq",
    state: "unconfigured",
    calls_today: 0,
    daily_limit: 60,
    trains_on_input: false,
    last_success_at: null,
    key_env: "GROQ_API_KEY",
  }),
];

const TOTALS = {
  capacity_left_today: 37,
  calls_today: 3,
  chat_reserve: 20,
  resets_at: "2026-11-08T08:00:00+00:00",
  generative_available: true,
  resumes_at: null,
  answers_today: {
    generative: 12,
    quoted: 3,
    abstained: 1,
    quoted_ai_unavailable: 2,
    since: "2026-11-07T00:00:00+00:00",
  },
};

function systemStatus(ai: Partial<SystemStatus["ai"]> = {}): SystemStatus {
  return {
    checked_at: "2026-11-07T12:00:00Z",
    components: {
      database: { status: "ok", latency_ms: 2 },
      cache: { status: "ok", latency_ms: 1 },
    },
    queues: { ingest: 0 },
    documents: { by_status: { COMPLETED: 4 }, archived: 0 },
    jobs_last_24h: { by_status: {}, timeouts: 0, stalled_now: 0 },
    users: { active: 3, inactive: 0 },
    ai: {
      embedding: { provider: "local", version: "bge", semantic: true },
      reranker: { name: "local-cross-encoder", version: "1", semantic: true },
      answerer: { name: "openai-compatible", version: "gemini/gemini-3.5-flash", generative: true },
      chunking: { strategy: "paragraph", version: "1", size: 1200, overlap: 200 },
      retrieval: { relevance_floor: 0.02 },
      enabled: true,
      chain: CHAIN,
      totals: TOTALS,
      ...ai,
    },
    pipeline: {},
    debug: false,
  };
}

function renderSystem() {
  return render(
    <MemoryRouter initialEntries={["/admin/system"]}>
      <AuthProvider>
        <ToastProvider>
          <App />
        </ToastProvider>
      </AuthProvider>
    </MemoryRouter>,
  );
}

async function card() {
  // Generous: the admin screens are a lazily loaded chunk.
  const heading = await screen.findByRole("heading", { name: "AI providers" }, { timeout: 15_000 });
  return heading.closest("section") as HTMLElement;
}

beforeEach(() => {
  vi.spyOn(api, "me").mockResolvedValue(ROOT);
  vi.spyOn(api, "adminSystem").mockResolvedValue(systemStatus());
  vi.spyOn(aiApi, "status").mockResolvedValue({
    checked_at: "2026-11-07T12:00:30Z",
    enabled: true,
    chain: CHAIN,
    totals: TOTALS,
  });
});

afterEach(() => vi.restoreAllMocks());

describe("System status · AI providers", () => {
  it("shows one row per link with its state, usage and what to do about it", async () => {
    renderSystem();
    const section = await card();

    expect(within(section).getByText(/≈37 generative answers left today · resets at/)).toBeInTheDocument();
    expect(within(section).getByText(/12 written · 3 quoted · 1 not answered · 2 quoted because no model could answer/)).toBeInTheDocument();

    const rows = within(section).getAllByRole("listitem");
    expect(rows).toHaveLength(3);
    expect(within(rows[0]).getByText("Gemini 3.5 Flash")).toBeInTheDocument();
    expect(within(rows[0]).getByText("Ready")).toBeInTheDocument();
    expect(within(rows[0]).getByText("May train on inputs")).toBeInTheDocument();
    expect(
      within(rows[0]).getByRole("meter", { name: "Gemini 3.5 Flash: 3 of 20 requests used today" }),
    ).toHaveAttribute("aria-valuenow", "0.15");
    expect(within(rows[1]).getByText("Cooling down")).toBeInTheDocument();
    expect(within(rows[1]).getByText(/Last error RateLimitError \(429\)/)).toBeInTheDocument();
    expect(within(rows[2]).getByText("No API key")).toBeInTheDocument();
    expect(within(rows[2]).getByText("Set GROQ_API_KEY on the server to use this model")).toBeInTheDocument();
  });

  it("keeps the dependency tiles and the configuration card the e2e suite reads", async () => {
    renderSystem();
    await card();
    expect(document.querySelectorAll(".health")).toHaveLength(2);
    expect(screen.getByText("Embeddings").closest(".dl-row")).toHaveTextContent("local");
  });

  it("tests one link and shows the answer in its row", async () => {
    const probe = vi.spyOn(aiApi, "test").mockResolvedValue({
      link: "gemini:gemini-3.5-flash",
      ok: true,
      status_code: null,
      error_type: null,
      model: "gemini-3.5-flash",
      latency_ms: 820,
      state: { ...CHAIN[0], calls_today: 4 },
    });
    renderSystem();
    const section = await card();

    await userEvent.click(within(section).getByRole("button", { name: "Test Gemini 3.5 Flash" }));

    expect(probe).toHaveBeenCalledWith("gemini:gemini-3.5-flash");
    const row = within(section).getAllByRole("listitem")[0];
    expect(await within(row).findByRole("status")).toHaveTextContent("Answered in");
    expect(within(row).getByText("4 / 20 today")).toBeInTheDocument();
  });

  it("says how a probe failed without inventing a reason", async () => {
    vi.spyOn(aiApi, "test").mockResolvedValue({
      link: "gemini:gemini-3-flash-preview",
      ok: false,
      status_code: 429,
      error_type: "RateLimitError",
      model: "",
      latency_ms: 300,
      state: CHAIN[1],
    });
    renderSystem();
    const section = await card();
    await userEvent.click(
      within(section).getByRole("button", { name: "Test Gemini 3 Flash (preview)" }),
    );
    expect(await within(section).findByRole("status")).toHaveTextContent(
      "Failed: RateLimitError (429)",
    );
  });

  it("says plainly when no model is configured", async () => {
    vi.mocked(api.adminSystem).mockResolvedValue(
      systemStatus({
        enabled: false,
        chain: [],
        totals: { ...TOTALS, capacity_left_today: 0, generative_available: false },
      }),
    );
    renderSystem();
    const section = await card();
    expect(within(section).getByText(/No generative model is configured/)).toBeInTheDocument();
    expect(within(section).queryByRole("button", { name: "Refresh" })).toBeNull();
  });
});

describe("AI providers summary", () => {
  const base: AIStatus = { checked_at: "", enabled: true, chain: CHAIN, totals: TOTALS };

  it("names when writing resumes once every model is at capacity", () => {
    const text = summary({
      ...base,
      totals: { ...TOTALS, generative_available: false, resumes_at: "2026-11-07T12:05:00Z" },
    });
    expect(text).toMatch(/^AI writing is paused; answers quote your documents until about /);
  });

  it("has no count to give when a model has no daily limit", () => {
    expect(summary({ ...base, totals: { ...TOTALS, capacity_left_today: null } })).toBe(
      "At least one model has no daily limit.",
    );
  });

  it("counts one answer in the singular", () => {
    expect(summary({ ...base, totals: { ...TOTALS, capacity_left_today: 1, resets_at: null } })).toBe(
      "≈1 generative answer left today",
    );
  });
});
