/**
 * Thin fetch wrapper around the Django API.
 *
 * Errors are raised, never swallowed into a default value: a console whose
 * job is to show what the backend did is the last place that should paper
 * over a failed call with an empty list.
 */

import type {
  ChatResponse,
  ClassificationResult,
  DocumentChunk,
  DocumentContent,
  DocumentSummary,
  DocumentType,
  DocumentVersion,
  ExtractionResult,
  Health,
  Me,
  Paginated,
  ProcessingJob,
  SearchResponse,
} from "./types";

const configured = import.meta.env.VITE_API_BASE_URL as string | undefined;

export const API_BASE_URL = (configured?.trim() || "http://localhost:8000").replace(
  /\/+$/,
  "",
);

const TOKEN_KEY = "adi.console.token";

export class ApiError extends Error {
  readonly status: number;
  readonly payload: unknown;

  constructor(status: number, message: string, payload: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }
}

/**
 * The token lives in localStorage. That is a deliberate development-tool
 * tradeoff, not a recommendation: a token there is readable by any script
 * on the origin. This console runs against a developer's own instance, and
 * the alternative - an httpOnly cookie - would need the session/CSRF path
 * that the API's CORS configuration intentionally does not open up.
 */
export function readToken(): string | null {
  try {
    return window.localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function writeToken(token: string | null): void {
  try {
    if (token === null) {
      window.localStorage.removeItem(TOKEN_KEY);
    } else {
      window.localStorage.setItem(TOKEN_KEY, token);
    }
  } catch {
    // Private browsing, or storage disabled. The session still works for
    // as long as the tab lives; it just will not survive a reload.
  }
}

function describe(status: number, payload: unknown): string {
  if (typeof payload === "string" && payload.trim()) return payload;

  if (payload && typeof payload === "object") {
    const body = payload as Record<string, unknown>;

    if (typeof body.detail === "string") return body.detail;

    // DRF field errors, e.g. {"file": ["No file was submitted."]}
    const parts = Object.entries(body).map(([key, value]) => {
      const text = Array.isArray(value) ? value.join(" ") : String(value);
      return `${key}: ${text}`;
    });

    if (parts.length) return parts.join(" · ");
  }

  return `Request failed with status ${status}`;
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  token?: string | null;
  signal?: AbortSignal;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, token, signal } = options;

  const headers: Record<string, string> = { Accept: "application/json" };
  if (token) headers.Authorization = `Token ${token}`;

  let payload: BodyInit | undefined;

  if (body instanceof FormData) {
    // Content-Type is left unset on purpose: the browser has to add the
    // multipart boundary, and setting the header by hand drops it.
    payload = body;
  } else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }

  const response = await fetch(`${API_BASE_URL}${path}`, {
    method,
    headers,
    body: payload,
    signal,
  });

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  let parsed: unknown = text;

  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      // Django's HTML debug page, or a proxy error. Keep the raw text.
    }
  }

  if (!response.ok) {
    throw new ApiError(response.status, describe(response.status, parsed), parsed);
  }

  return parsed as T;
}

export interface UploadInput {
  title: string;
  document_type: DocumentType;
  department?: string;
  file: File;
}

export interface SearchInput {
  query: string;
  limit?: number;
  document_type?: string;
  department?: string;
}

export interface ChatInput {
  question: string;
  conversation_id?: number | null;
  document_type?: string;
  department?: string;
}

export const api = {
  health: () => request<Health>("/health/"),

  login: (username: string, password: string) =>
    request<{ token: string }>("/api/auth/token/", {
      method: "POST",
      body: { username, password },
    }),

  me: (token: string) => request<Me>("/api/auth/me/", { token }),

  documents: (token: string, page = 1) =>
    request<Paginated<DocumentSummary>>(`/api/documents/?page=${page}`, { token }),

  document: (token: string, id: number) =>
    request<DocumentSummary>(`/api/documents/${id}/`, { token }),

  upload: (token: string, input: UploadInput) => {
    const form = new FormData();
    form.append("title", input.title);
    form.append("document_type", input.document_type);
    if (input.department) form.append("department", input.department);
    form.append("file", input.file);

    return request<DocumentSummary>("/api/documents/", {
      method: "POST",
      body: form,
      token,
    });
  },

  updateDocument: (
    token: string,
    id: number,
    patch: { title?: string; document_type?: DocumentType },
  ) => {
    // The viewset parses multipart/form-data only, so a PATCH has to be
    // form-encoded even though it carries no file.
    const form = new FormData();
    if (patch.title !== undefined) form.append("title", patch.title);
    if (patch.document_type !== undefined) {
      form.append("document_type", patch.document_type);
    }

    return request<DocumentSummary>(`/api/documents/${id}/`, {
      method: "PATCH",
      body: form,
      token,
    });
  },

  deleteDocument: (token: string, id: number) =>
    request<void>(`/api/documents/${id}/`, { method: "DELETE", token }),

  versions: (token: string, id: number) =>
    request<DocumentVersion[]>(`/api/documents/${id}/versions/`, { token }),

  uploadVersion: (token: string, id: number, file: File) => {
    const form = new FormData();
    form.append("file", file);

    return request<DocumentVersion>(`/api/documents/${id}/versions/`, {
      method: "POST",
      body: form,
      token,
    });
  },

  processing: (token: string, id: number) =>
    request<ProcessingJob[]>(`/api/documents/${id}/processing/`, { token }),

  reprocess: (token: string, id: number) =>
    request<ProcessingJob>(`/api/documents/${id}/reprocess/`, {
      method: "POST",
      token,
    }),

  content: (token: string, id: number) =>
    request<DocumentContent>(`/api/documents/${id}/content/`, { token }),

  classification: (token: string, id: number) =>
    request<ClassificationResult[]>(`/api/documents/${id}/classification/`, { token }),

  extraction: (token: string, id: number) =>
    request<ExtractionResult[]>(`/api/documents/${id}/extraction/`, { token }),

  chunks: (token: string, id: number, page = 1) =>
    request<Paginated<DocumentChunk>>(`/api/documents/${id}/chunks/?page=${page}`, {
      token,
    }),

  search: (token: string, input: SearchInput) =>
    request<SearchResponse>("/api/search/", { method: "POST", body: input, token }),

  chat: (token: string, input: ChatInput) =>
    request<ChatResponse>("/api/chat/", { method: "POST", body: input, token }),

  /**
   * Downloads through fetch rather than a plain link: the bytes are behind
   * token auth, and an anchor href cannot carry an Authorization header.
   */
  download: async (token: string, id: number, fallbackName: string) => {
    const response = await fetch(`${API_BASE_URL}/api/documents/${id}/download/`, {
      headers: { Authorization: `Token ${token}` },
    });

    if (!response.ok) {
      throw new ApiError(response.status, `Download failed (${response.status})`, null);
    }

    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = fallbackName;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  },
};
