import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { analyticsApi, type AnalyticsReport, type EvaluationRun, type FeedbackItem, type Kpi } from "../../api/analytics";
import { ApiError, api } from "../../api/client";
import type { Me } from "../../api/types";
import App from "../../App";
import { AuthProvider } from "../../auth/AuthContext";
import { ToastProvider } from "../../components/ui";
import { formatLongDay } from "../../lib/format";

const ADMIN: Me = {
  id: 1,
  username: "cs_admin",
  email: "",
  first_name: "Meera",
  last_name: "Iyer",
  is_superuser: false,
  role: "DEPARTMENT_ADMIN",
  department: "Computer Science",
  capabilities: {
    administer_users: true,
    administer_departments: false,
    view_audit_log: true,
    view_operations: true,
    view_system_status: false,
    upload_to_any_department: false,
    create_collections: false,
  },
};

const DAYS = ["2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06", "2026-11-07"];

const kpi = (series: (number | null)[], previous: number | null, unit: Kpi["unit"] = "count"): Kpi => {
  const value =
    unit === "ratio" ? (series.filter((v) => v !== null).at(-1) ?? null) : series.reduce<number>((s, v) => s + (v ?? 0), 0);
  return { value, previous, delta: value === null || previous === null ? null : value - previous, unit, series };
};

function report(empty = false): AnalyticsReport {
  const n = (values: number[]) => (empty ? values.map(() => 0) : values);
  const answered = n([1, 2, 0, 3, 1, 2, 4]);
  return {
    range: {
      key: "7d",
      days: 7,
      start: DAYS[0],
      end: DAYS[6],
      previous_start: "2026-10-25",
      previous_end: "2026-10-31",
      time_zone: "UTC",
    },
    generated_at: new Date().toISOString(),
    cache_seconds: 300,
    scope: { department: "Computer Science" },
    kpis: {
      questions: kpi(n([2, 2, 0, 4, 1, 2, 5]), empty ? 0 : 10),
      answer_rate: kpi(empty ? DAYS.map(() => null) : [0.5, 1, null, 0.75, 1, 1, 0.8], empty ? null : 0.7, "ratio"),
      helpful_rate: kpi(DAYS.map(() => null), null, "ratio"),
      documents_processed: kpi(n([0, 1, 0, 0, 2, 0, 1]), empty ? 0 : 3),
      documents_failed: kpi(n([0, 0, 0, 1, 0, 0, 0]), 0),
      active_users: kpi(n([1, 2, 1, 1, 2, 1, 3]), empty ? 0 : 4),
      searches: kpi(n([3, 1, 0, 0, 2, 0, 4]), empty ? 0 : 6),
      zero_hit_rate: kpi(DAYS.map(() => null), null, "ratio"),
      storage_bytes: { value: empty ? 0 : 4096, previous: 0, delta: empty ? 0 : 4096, unit: "bytes", series: DAYS.map(() => 0) },
    },
    questions: {
      series: DAYS.map((date, i) => ({
        date,
        total: answered[i] + 1,
        answered: answered[i],
        general: 0,
        abstained: empty ? 0 : 1,
        generative: 0,
        quoted: answered[i],
      })),
    },
    latency: {
      series: DAYS.map((date) => ({ date, p50_ms: empty ? null : 1200, p95_ms: empty ? null : 4000, answers: empty ? 0 : 2 })),
      overall: { p50_ms: empty ? null : 1200, p95_ms: empty ? null : 4000, answers: empty ? 0 : 14 },
      stages: empty ? [] : [{ stage: "retrieval", median_ms: 600, answers: 14 }],
    },
    abstentions: empty
      ? []
      : [{ code: "not_relevant", reason: "retrieved passages were not relevant enough", count: 7 }],
    feedback: {
      series: DAYS.map((date) => ({ date, helpful: 0, not_helpful: 0, incorrect: 0 })),
      totals: { helpful: 0, not_helpful: 0, incorrect: 0 },
      flagged: [],
    },
    documents: {
      uploads: DAYS.map((date) => ({ date, count: empty ? 0 : 1, bytes: 100 })),
      processing: DAYS.map((date) => ({ date, completed: empty ? 0 : 1, failed: 0 })),
      failures: empty ? [] : [{ error_code: "OCR_FAILED", count: 1 }],
      stages: empty ? [] : [{ stage: "OCR", p50_seconds: 3.2, p95_seconds: 9.5, runs: 4 }],
      by_type: empty ? [] : [{ document_type: "POLICY", count: 3 }],
      by_department: [],
      storage: { total_bytes: 4096, files: 3, by_department: [], by_kind: [{ kind: "PDF", bytes: 4096, files: 3 }] },
    },
    usage: {
      active_users: DAYS.map((date) => ({ date, count: empty ? 0 : 1 })),
      searches: DAYS.map((date) => ({ date, count: empty ? 0 : 2, zero_hit: 0 })),
      top_cited: empty ? [] : [{ document_id: 12, title: "Lab rules", answers: 5, citations: 8 }],
      most_viewed: [],
    },
  };
}

/** A report that asked `questions` questions, to tell one from another on screen. */
function withQuestions(questions: number): AnalyticsReport {
  const base = report();
  return { ...base, kpis: { ...base.kpis, questions: kpi([questions, 0, 0, 0, 0, 0, 0], 10) } };
}

const FLAG: FeedbackItem = {
  id: 3,
  rating: "INCORRECT",
  comment: "It closes at 8.",
  rated_on: "2026-11-07",
  department: "Computer Science",
  question: null,
  answer_excerpt: "The lab closes at 6 pm. [1]",
  answer_withheld: false,
  abstained: false,
  abstention_reason: "",
  citations: 1,
  source: "documents",
  generative: false,
  model: "extractive-answerer",
  support_ratio: 1,
  documents: [{ id: 12, title: "Lab rules" }],
};

const page = <T,>(results: T[]) => ({ count: results.length, next: null, previous: null, results });

function renderAt(path = "/admin/analytics") {
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
  vi.spyOn(api, "me").mockResolvedValue(ADMIN);
  vi.spyOn(analyticsApi, "feedback").mockResolvedValue(page([FLAG]));
  vi.spyOn(analyticsApi, "evaluationRuns").mockResolvedValue(page<EvaluationRun>([]));
});

afterEach(() => vi.restoreAllMocks());

const heading = () => screen.findByRole("heading", { name: "Analytics", level: 1 }, { timeout: 15_000 });

describe("AnalyticsPage", () => {
  it("shows the headline numbers for the department, with their change", async () => {
    vi.spyOn(analyticsApi, "report").mockResolvedValue(report());
    renderAt();
    await heading();
    expect(await screen.findByText("Questions asked")).toBeInTheDocument();
    expect(screen.getByText(/Computer Science department/)).toBeInTheDocument();
    // 16 questions against 10: +60%.
    expect(screen.getByText("16")).toBeInTheDocument();
    expect(screen.getByText("+60%")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Questions per day by outcome" })).toBeInTheDocument();
    expect(screen.getByText(/Updated/)).toBeInTheDocument();
    // First in the Administration group of the sidebar.
    expect(screen.getAllByRole("link", { name: "Analytics" }).length).toBeGreaterThan(0);
  });

  it("switches the period and keeps it in the address", async () => {
    const load = vi.spyOn(analyticsApi, "report").mockResolvedValue(report());
    renderAt();
    await heading();
    const period = await screen.findByRole("group", { name: "Period" });
    expect(within(period).getByRole("button", { name: "30 days" })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(within(period).getByRole("button", { name: "7 days" }));
    await waitFor(() => expect(load).toHaveBeenLastCalledWith("7d"));
    expect(within(period).getByRole("button", { name: "7 days" })).toHaveAttribute("aria-pressed", "true");
  });

  it("refreshes on request, and says so when that is throttled", async () => {
    const load = vi.spyOn(analyticsApi, "report").mockResolvedValue(report());
    renderAt("/admin/analytics?range=90d");
    await heading();
    await screen.findByText("Questions asked");
    expect(load).toHaveBeenCalledWith("90d");
    load.mockRejectedValueOnce(new ApiError(429, "Too many requests.", null, 60));
    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(load).toHaveBeenLastCalledWith("90d", true);
    expect(await screen.findByText(/Refreshed several times already/)).toBeInTheDocument();
  });

  it("puts a refreshed report only on the period it was asked for", async () => {
    let deliver: (value: AnalyticsReport) => void = () => undefined;
    const recomputed = new Promise<AnalyticsReport>((resolve) => (deliver = resolve));
    const load = vi
      .spyOn(analyticsApi, "report")
      .mockImplementation((range, fresh) =>
        fresh ? recomputed : Promise.resolve(range === "7d" ? withQuestions(723) : report()),
      );
    renderAt();
    await heading();
    expect(await screen.findByText("16")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(load).toHaveBeenLastCalledWith("30d", true);
    // The recompute is slow and throttled; the period changes meanwhile.
    await userEvent.click(within(screen.getByRole("group", { name: "Period" })).getByRole("button", { name: "7 days" }));
    expect(await screen.findByText("723")).toBeInTheDocument();
    await act(async () => deliver(withQuestions(99)));

    // The 30-day figures are not shown under "7 days".
    expect(screen.getByText("723")).toBeInTheDocument();
    expect(screen.queryByText("99")).not.toBeInTheDocument();
  });

  it("looks intentional on a new installation", async () => {
    vi.spyOn(analyticsApi, "report").mockResolvedValue(report(true));
    renderAt();
    await heading();
    expect(await screen.findByText("Nothing has happened in this period yet")).toBeInTheDocument();
    expect(screen.getByText("No questions were asked in this period.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to documents" })).toHaveAttribute("href", "/documents");
  });

  it("lists rated answers and benchmark runs on the quality tab", async () => {
    vi.spyOn(analyticsApi, "report").mockResolvedValue(report());
    renderAt("/admin/analytics?tab=quality");
    await heading();
    expect(await screen.findByText("“It closes at 8.”", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("The lab closes at 6 pm. [1]")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Lab rules" })).toHaveAttribute("href", "/documents/12");
    // Dated to the day it was rated: the queue is sent no time to show.
    expect(document.querySelector('.feedback-item time[datetime="2026-11-07"]')).toHaveTextContent(
      formatLongDay("2026-11-07"),
    );
    expect(screen.getByText(/Questions are visible to system administrators only/)).toBeInTheDocument();
    expect(screen.getByText("Nothing relevant enough")).toBeInTheDocument();
    expect(await screen.findByText("No benchmark runs yet")).toBeInTheDocument();
    expect(analyticsApi.feedback).toHaveBeenCalledWith({ rating: "INCORRECT,NOT_HELPFUL", page: 1 });
  });

  it("names failures in plain words on the documents tab", async () => {
    vi.spyOn(analyticsApi, "report").mockResolvedValue(report());
    renderAt("/admin/analytics?tab=documents");
    await heading();
    expect(await screen.findByText("Scanned text could not be recognised")).toBeInTheDocument();
    expect(screen.getByRole("rowheader", { name: "OCR" })).toBeInTheDocument();
  });

  it("is not offered to someone who cannot use it", async () => {
    vi.spyOn(api, "me").mockResolvedValue({
      ...ADMIN,
      role: "STUDENT",
      capabilities: { ...ADMIN.capabilities, administer_users: false, view_audit_log: false, view_operations: false },
    });
    const load = vi.spyOn(analyticsApi, "report").mockResolvedValue(report());
    renderAt();
    expect(await screen.findByText("You do not have access to this page", {}, { timeout: 15_000 })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Analytics" })).not.toBeInTheDocument();
    expect(load).not.toHaveBeenCalled();
  });
});
