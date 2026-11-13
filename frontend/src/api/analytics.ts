/**
 * Analytics, the answer-feedback review queue, evaluation runs, and the
 * dashboard's activity block. Shapes mirror the server exactly - see
 * docs/api/analytics.md.
 */
import { request } from "./client";
import type { DocumentType, FeedbackRating, Paginated } from "./types";

export type AnalyticsRange = "7d" | "30d" | "90d";

/** A headline number against the previous period of the same length. */
export interface Kpi {
  value: number | null;
  previous: number | null;
  /** value - previous; a ratio's in fraction points (0.03 = 3 points). */
  delta: number | null;
  unit: "count" | "ratio" | "bytes";
  /** One value per day of the range; null where a rate had nothing to measure. */
  series: (number | null)[];
}

export interface RangeInfo {
  key: string;
  days: number;
  /** "YYYY-MM-DD" days in `time_zone`, inclusive. */
  start: string;
  end: string;
  previous_start: string;
  previous_end: string;
  time_zone: string;
}

export interface FeedbackItem {
  id: number;
  rating: FeedbackRating;
  comment: string;
  /**
   * "YYYY-MM-DD", the day it was rated, in the analytics time zone. Never a
   * time: the audit log's CHAT row for the answer would name the rater.
   */
  rated_on: string;
  department: string;
  /** Only for a system administrator, and only on a flag; null otherwise. */
  question: string | null;
  answer_excerpt: string;
  /** The answer quotes a document outside the reader's department. */
  answer_withheld: boolean;
  abstained: boolean;
  abstention_reason: string;
  citations: number;
  source: "documents" | "general" | "none";
  generative: boolean;
  model: string;
  support_ratio: number;
  documents: { id: number; title: string }[];
}

export interface AnalyticsReport {
  range: RangeInfo;
  generated_at: string;
  cache_seconds: number;
  /** null: every department (a system administrator). */
  scope: { department: string | null };
  kpis: {
    questions: Kpi;
    answer_rate: Kpi;
    helpful_rate: Kpi;
    documents_processed: Kpi;
    documents_failed: Kpi;
    active_users: Kpi;
    searches: Kpi;
    zero_hit_rate: Kpi;
    storage_bytes: Kpi;
  };
  questions: {
    series: {
      date: string;
      total: number;
      answered: number;
      general: number;
      abstained: number;
      generative: number;
      quoted: number;
    }[];
  };
  latency: {
    series: { date: string; p50_ms: number | null; p95_ms: number | null; answers: number }[];
    overall: { p50_ms: number | null; p95_ms: number | null; answers: number };
    stages: { stage: string; median_ms: number | null; answers: number }[];
  };
  abstentions: { code: string; reason: string; count: number }[];
  feedback: {
    series: { date: string; helpful: number; not_helpful: number; incorrect: number }[];
    totals: { helpful: number; not_helpful: number; incorrect: number };
    flagged: FeedbackItem[];
  };
  documents: {
    uploads: { date: string; count: number; bytes: number }[];
    processing: { date: string; completed: number; failed: number }[];
    failures: { error_code: string; count: number }[];
    stages: { stage: string; p50_seconds: number; p95_seconds: number; runs: number }[];
    by_type: { document_type: DocumentType; count: number }[];
    by_department: { department: string; count: number }[];
    storage: {
      total_bytes: number;
      files: number;
      by_department: { department: string; bytes: number; files: number }[];
      by_kind: { kind: string; bytes: number; files: number }[];
    };
  };
  usage: {
    active_users: { date: string; count: number }[];
    searches: { date: string; count: number; zero_hit: number }[];
    top_cited: { document_id: number; title: string; answers: number; citations: number }[];
    most_viewed: { document_id: number; title: string; readers: number; visits: number }[];
  };
}

/** GET /api/v1/dashboard/ `activity` - see docs/api/analytics.md. */
export interface DashboardActivity {
  generated_at: string;
  time_zone: string;
  /** 12 weeks, oldest first; `week` is the Monday. */
  uploads_by_week: { week: string; count: number }[];
  /** 30 days, oldest first; the reader's own questions. */
  questions_by_day: { date: string; count: number; answered: number }[];
  documents_by_type: { document_type: DocumentType; count: number }[];
  documents_by_department: { department: string; count: number }[];
  /** Administrators only (view_operations); null for everyone else. */
  org: {
    range: RangeInfo;
    generated_at: string;
    scope: { department: string | null };
    kpis: {
      questions: Kpi;
      answer_rate: Kpi;
      helpful_rate: Kpi;
      documents_processed: Kpi;
    };
  } | null;
}

export interface EvaluationRun {
  id: number;
  dataset: {
    id: number;
    name: string;
    version: string;
    description: string;
    department: string;
    cases: number;
    created_at: string;
  };
  label: string;
  status: "RUNNING" | "COMPLETED" | "FAILED";
  configuration: Record<string, unknown>;
  metrics: {
    retrieval?: {
      recall_at_k?: Record<string, number>;
      precision_at_k?: Record<string, number>;
      ndcg_at_k?: Record<string, number>;
      mrr?: number;
      map?: number;
      cases?: number;
    };
    generation?: Record<string, number>;
    system?: Record<string, number>;
  };
  cases_total: number;
  cases_scored: number;
  duration_ms: number;
  error_message: string;
  started_at: string;
  completed_at: string | null;
}

function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export const analyticsApi = {
  /** `fresh` recomputes instead of serving the cached report (throttled). */
  report: (range: AnalyticsRange, fresh = false) =>
    request<AnalyticsReport>(`/api/v1/admin/analytics/${query({ range, fresh: fresh ? 1 : undefined })}`),
  /** `rating`: one rating or several, comma-separated. */
  feedback: (params: { rating?: string; since?: string; page?: number } = {}) =>
    request<Paginated<FeedbackItem>>(`/api/v1/admin/feedback/${query(params)}`),
  evaluationRuns: (page = 1) =>
    request<Paginated<EvaluationRun>>(`/api/v1/evaluations/${query({ page: page > 1 ? page : undefined })}`),
};
