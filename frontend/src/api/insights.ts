/**
 * The document-intelligence API: a document's brief, its related documents,
 * its first-page thumbnail, and the corpus-level suggestions and deadlines.
 * Shapes mirror backend/documents/api/insights.py (docs/api/reference.md,
 * "Document intelligence").
 *
 * An object of functions rather than loose exports, so a test can replace
 * one call with vi.spyOn(insightsApi, "insights").
 */

import { API_BASE_URL, request } from "./client";
import type { DocumentType, TypeSource } from "./types";

export type InsightStatus = "READY" | "PENDING" | "FAILED" | "UNAVAILABLE";

/** A span of the stored page text: Unicode code-point offsets. */
export interface Span {
  text: string;
  page_number: number;
  start: number;
  end: number;
}

export type KeyPointKind =
  | "deadline"
  | "obligation"
  | "figure"
  | "date"
  | "clause"
  | "heading";

export interface KeyPoint extends Span {
  kind: KeyPointKind;
}

export type EntityType =
  | "DATE"
  | "DEADLINE"
  | "MONEY"
  | "PERCENT"
  | "DURATION"
  | "EMAIL"
  | "PHONE"
  | "URL"
  | "REFERENCE"
  | "COURSE_CODE"
  | "ORG";

export interface InsightEntity {
  type: EntityType;
  text: string;
  /** ISO date, "INR 4500", "+919876543210", "P15D"... or null. */
  normalized: string | null;
  page_number: number;
  start: number;
  end: number;
  /** How many times it occurs; the span is the first occurrence. */
  count: number;
  /** What a date or deadline is for, in the document's words; "" if nothing. */
  label: string;
}

export interface InsightStats {
  pages: number;
  words: number;
  reading_minutes: number;
  ocr_pages: number;
  language: "en" | "hi" | "te" | "mixed" | "unknown";
}

export interface DocumentInsights {
  document_id: number;
  version_id: number | null;
  content_id: number | null;
  status: InsightStatus;
  method: string;
  generated_at: string | null;
  summary: Span[];
  key_points: KeyPoint[];
  key_phrases: { text: string; score: number }[];
  entities: InsightEntity[];
  stats: InsightStats;
  suggested_questions: string[];
  document_type: { value: DocumentType; source: TypeSource; confidence: number | null };
  /** Reserved for an on-demand AI brief; null until one exists. */
  ai: { summary: string; questions: string[]; model: string; generated_at: string } | null;
}

export interface RelatedDocument {
  id: number;
  title: string;
  document_type: DocumentType;
  department: string;
  updated_at: string;
  similarity: number;
  near_duplicate: boolean;
}

export interface RelatedDocuments {
  results: RelatedDocument[];
  method: "semantic" | "lexical" | "unavailable";
}

export interface Suggestion {
  question: string;
  document_id: number;
  document_title: string;
}

export interface Deadline extends Omit<Span, "text"> {
  date: string;
  type: "DEADLINE" | "DATE";
  label: string;
  text: string;
  document_id: number;
  document_title: string;
}


const doc = (id: number, suffix: string) => `/api/v1/documents/${id}/${suffix}`;

export const insightsApi = {
  insights: (id: number) => request<DocumentInsights>(doc(id, "insights/")),
  related: (id: number) => request<RelatedDocuments>(doc(id, "related/")),
  suggestions: () => request<{ results: Suggestion[] }>("/api/v1/documents/suggestions/"),
  deadlines: (days = 90) =>
    request<{ results: Deadline[] }>(`/api/v1/documents/deadlines/?days=${days}`),
};

/**
 * The thumbnail's URL for an <img>. Same-origin, so the session cookie goes
 * with it and the CSP's img-src 'self' allows it. The version id only keys
 * the browser cache: a new version is a new URL, never a stale picture.
 */
export function thumbnailUrl(documentId: number, versionId?: number | null): string {
  const key = versionId ? `?v=${versionId}` : "";
  return `${API_BASE_URL}${doc(documentId, "thumbnail/")}${key}`;
}

/** The deep link that opens the Text tab with this span highlighted. */
export function spanLink(documentId: number, span: Omit<Span, "text">): string {
  return `/documents/${documentId}?tab=text&page=${span.page_number}&start=${span.start}&end=${span.end}`;
}
