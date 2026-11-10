/**
 * What the home screen reads besides GET /api/v1/dashboard/. Each of these
 * is an optional extra: an endpoint this server does not have (404), or one
 * that fails, reads as "nothing to show" - the dashboard never breaks
 * because a suggestion or a deadline could not be fetched.
 */
import { ApiError, request } from "./client";
import type { Paginated } from "./types";

export interface Suggestion {
  question: string;
  document_id: number;
  document_title: string;
}

export interface Deadline {
  /** "YYYY-MM-DD". */
  date: string;
  label: string;
  /** The sentence the date was found in. */
  text: string;
  document_id: number;
  document_title: string;
  page_number: number;
  /** Unicode code-point offsets into that page's text. */
  start: number;
  end: number;
}

export interface SampleLoad {
  queued: number;
  skipped: number;
}

/** null on any failure: the caller shows nothing rather than an error. */
async function optional<T>(load: () => Promise<T>): Promise<T | null> {
  try {
    return await load();
  } catch (caught) {
    if (caught instanceof DOMException && caught.name === "AbortError") throw caught;
    return null;
  }
}

export const homeApi = {
  /** Questions built from the reader's own documents; [] when unavailable. */
  suggestions: async (): Promise<Suggestion[]> =>
    (await optional(() => request<{ results: Suggestion[] }>("/api/v1/documents/suggestions/")))?.results ?? [],
  /** Dates found in the reader's documents in the next `days` days; [] when unavailable. */
  deadlines: async (days = 90): Promise<Deadline[]> =>
    (await optional(() => request<{ results: Deadline[] }>(`/api/v1/documents/deadlines/?days=${days}`)))?.results ??
    [],
  /** How many collections the reader can see; null when unknown. */
  collectionCount: async (): Promise<number | null> =>
    (await optional(() => request<Paginated<unknown>>("/api/v1/collections/")))?.count ?? null,
  /**
   * Whether this server can load sample documents. Asked with a GET, which
   * the endpoint does not serve: 405 means it exists, 404 that it does not.
   */
  samplesAvailable: async (): Promise<boolean> => {
    try {
      await request<unknown>("/api/v1/admin/samples/");
      return true;
    } catch (caught) {
      return caught instanceof ApiError && caught.status === 405;
    }
  },
  loadSamples: (department: string) =>
    request<SampleLoad>("/api/v1/admin/samples/", { method: "POST", body: { department } }),
};

/** The text tab of a document, opened at a passage. */
export function passageLink(deadline: Pick<Deadline, "document_id" | "page_number" | "start" | "end">): string {
  const params = new URLSearchParams({
    tab: "text",
    page: String(deadline.page_number),
    start: String(deadline.start),
    end: String(deadline.end),
  });
  return `/documents/${deadline.document_id}?${params.toString()}`;
}
