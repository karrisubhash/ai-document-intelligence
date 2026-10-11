/**
 * The API client.
 *
 * Same-origin, cookie-authenticated. The browser holds an httpOnly session
 * cookie that no script here can read, and every unsafe request carries the
 * CSRF token Django issued. Nothing credential-like is ever written to
 * localStorage - one XSS would otherwise walk out with every signed-in
 * user's access.
 *
 * Errors are raised, never swallowed into a default value. A screen that
 * turns a failed call into an empty list tells the user "there is nothing
 * here" when the truth is "we could not look".
 */

import type {
  AdminDepartment,
  AdminUser,
  AuditEntry,
  ChatResponse,
  ClassificationResult,
  Conversation,
  ConversationSummary,
  Dashboard,
  Department,
  DocumentContent,
  DocumentDetail,
  DocumentFigure,
  DocumentSummary,
  DocumentTable,
  DocumentType,
  DocumentVersion,
  ExtractionResult,
  FeedbackRating,
  Me,
  OperationsJob,
  Paginated,
  ProcessingJob,
  RoleOption,
  SearchResponse,
  SourcePassage,
  SystemStatus,
} from "./types";

/** Empty in the browser (same origin). Settable for tests. */
export const API_BASE_URL = (
  (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? ""
).replace(/\/+$/, "");

export class ApiError extends Error {
  readonly status: number;
  readonly payload: unknown;

  constructor(status: number, message: string, payload: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }

  /** DRF field errors, e.g. {"title": ["This field is required."]}. */
  fieldErrors(): Record<string, string> {
    const errors: Record<string, string> = {};
    if (this.payload && typeof this.payload === "object") {
      for (const [key, value] of Object.entries(this.payload as object)) {
        if (key === "detail") continue;
        errors[key] = Array.isArray(value) ? value.join(" ") : String(value);
      }
    }
    return errors;
  }
}

/** Called once per request that comes back 401, so the session can end. */
let onUnauthorized: (() => void) | null = null;

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  onUnauthorized = handler;
}

const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export function readCookie(name: string): string {
  const match = document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(`${name}=`));
  return match ? decodeURIComponent(match.slice(name.length + 1)) : "";
}

let csrfPromise: Promise<void> | null = null;

/**
 * Makes sure the CSRF cookie exists before the first unsafe request.
 * Django sets it on GET /api/v1/auth/csrf/; afterwards it is read from the
 * cookie on every request, because login rotates it.
 */
export function ensureCsrf(): Promise<void> {
  if (readCookie("csrftoken")) return Promise.resolve();
  if (!csrfPromise) {
    csrfPromise = fetch(`${API_BASE_URL}/api/v1/auth/csrf/`, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    })
      .then(
        () => undefined,
        // The status-0 error every other unreachable call raises. A bare
        // TypeError reached the sign-in screen as "Failed to fetch".
        () => {
          throw new ApiError(0, describe(0, null), null);
        },
      )
      .finally(() => {
        csrfPromise = null;
      });
  }
  return csrfPromise;
}

export function describe(status: number, payload: unknown): string {
  // A short plain-text body is a message for a person; an error PAGE is
  // not. Django's default 500 page and nginx's 413/502/503/504 pages are
  // each under 300 characters of markup, and were shown tag by tag.
  if (
    typeof payload === "string" &&
    payload.trim() &&
    payload.length < 300 &&
    !payload.trimStart().startsWith("<")
  ) {
    return payload;
  }
  if (payload && typeof payload === "object") {
    const body = payload as Record<string, unknown>;
    if (typeof body.detail === "string") return body.detail;
    const parts = Object.entries(body).map(([key, value]) => {
      const text = Array.isArray(value) ? value.join(" ") : String(value);
      return key === "non_field_errors" ? text : `${key}: ${text}`;
    });
    if (parts.length) return parts.join(" · ");
  }
  if (status === 0) return "The server could not be reached.";
  if (status === 429) return "Too many requests. Please wait a moment and try again.";
  if (status >= 500) return "The server had a problem handling this request.";
  return `Request failed (${status}).`;
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  signal?: AbortSignal;
  /** Skip the global 401 handler - for the call that checks the session. */
  quiet?: boolean;
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, signal, quiet = false } = options;
  const headers: Record<string, string> = { Accept: "application/json" };

  if (UNSAFE.has(method)) {
    await ensureCsrf();
    headers["X-CSRFToken"] = readCookie("csrftoken");
  }

  let payload: BodyInit | undefined;
  if (body instanceof FormData) {
    // The browser adds the multipart boundary; setting the header by hand
    // would drop it.
    payload = body;
  } else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }

  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      method,
      headers,
      body: payload,
      signal,
      credentials: "same-origin",
    });
  } catch (caught) {
    if (caught instanceof DOMException && caught.name === "AbortError") throw caught;
    throw new ApiError(0, describe(0, null), null);
  }

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  let parsed: unknown = text;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      // A proxy error page, not JSON. Kept as text for describe().
    }
  }

  if (!response.ok) {
    if (response.status === 401 && !quiet) onUnauthorized?.();
    throw new ApiError(response.status, describe(response.status, parsed), parsed);
  }

  return parsed as T;
}

function query(params: Record<string, string | number | boolean | undefined | null>) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export interface DocumentQuery {
  page?: number;
  search?: string;
  status?: string;
  document_type?: string;
  department?: string;
  owner?: string;
  ordering?: string;
  archived?: "false" | "true" | "all";
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

/**
 * Uploads through XMLHttpRequest rather than fetch, because fetch still
 * cannot report upload progress - and a 40 MB scan with no progress bar
 * looks exactly like a frozen page.
 */
export function uploadWithProgress<T>(
  path: string,
  form: FormData,
  onProgress: (fraction: number) => void,
): Promise<T> {
  return ensureCsrf().then(
    () =>
      new Promise<T>((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open("POST", `${API_BASE_URL}${path}`);
        xhr.setRequestHeader("Accept", "application/json");
        xhr.setRequestHeader("X-CSRFToken", readCookie("csrftoken"));
        xhr.upload.onprogress = (event) => {
          if (event.lengthComputable) onProgress(event.loaded / event.total);
        };
        xhr.onerror = () => reject(new ApiError(0, describe(0, null), null));
        xhr.onload = () => {
          let parsed: unknown = xhr.responseText;
          try {
            parsed = JSON.parse(xhr.responseText);
          } catch {
            // Non-JSON error page; describe() handles text.
          }
          if (xhr.status >= 200 && xhr.status < 300) {
            resolve(parsed as T);
          } else {
            if (xhr.status === 401) onUnauthorized?.();
            reject(new ApiError(xhr.status, describe(xhr.status, parsed), parsed));
          }
        };
        xhr.send(form);
      }),
  );
}

/** Fetches a file as a Blob - for previews and downloads behind the session. */
export async function fetchBlob(path: string, signal?: AbortSignal): Promise<Blob> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, { credentials: "same-origin", signal });
  } catch (caught) {
    // As in request(): a download that cannot reach the server said
    // "Failed to fetch".
    if (caught instanceof DOMException && caught.name === "AbortError") throw caught;
    throw new ApiError(0, describe(0, null), null);
  }
  if (!response.ok) {
    if (response.status === 401) onUnauthorized?.();
    let parsed: unknown = null;
    try {
      parsed = await response.json();
    } catch {
      // body was not JSON
    }
    throw new ApiError(response.status, describe(response.status, parsed), parsed);
  }
  return response.blob();
}

export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

const doc = (id: number, suffix = "") => `/api/v1/documents/${id}/${suffix}`;

export const api = {
  // --- identity --------------------------------------------------------
  me: () => request<Me>("/api/v1/auth/me/", { quiet: true }),
  login: (username: string, password: string) =>
    request<Me>("/api/v1/auth/login/", {
      method: "POST",
      body: { username, password },
      quiet: true,
    }),
  logout: () => request<void>("/api/v1/auth/logout/", { method: "POST", quiet: true }),
  changePassword: (current_password: string, new_password: string) =>
    request<void>("/api/v1/auth/password/", {
      method: "POST",
      body: { current_password, new_password },
    }),
  departments: () => request<Department[]>("/api/v1/departments/"),
  dashboard: () => request<Dashboard>("/api/v1/dashboard/"),

  // --- documents -------------------------------------------------------
  documents: (params: DocumentQuery = {}) =>
    request<Paginated<DocumentSummary>>(`/api/v1/documents/${query({ ...params })}`),
  document: (id: number) => request<DocumentDetail>(doc(id)),
  upload: (input: UploadInput, onProgress: (fraction: number) => void) => {
    const form = new FormData();
    form.append("title", input.title);
    form.append("document_type", input.document_type);
    if (input.department) form.append("department", input.department);
    form.append("file", input.file);
    return uploadWithProgress<DocumentDetail>("/api/v1/documents/", form, onProgress);
  },
  uploadVersion: (id: number, file: File, onProgress: (fraction: number) => void) => {
    const form = new FormData();
    form.append("file", file);
    return uploadWithProgress<DocumentVersion>(doc(id, "versions/"), form, onProgress);
  },
  updateDocument: (id: number, patch: { title?: string; document_type?: DocumentType }) => {
    // The viewset parses multipart only, so a PATCH is form-encoded.
    const form = new FormData();
    if (patch.title !== undefined) form.append("title", patch.title);
    if (patch.document_type !== undefined) form.append("document_type", patch.document_type);
    return request<DocumentDetail>(doc(id), { method: "PATCH", body: form });
  },
  deleteDocument: (id: number) => request<void>(doc(id), { method: "DELETE" }),
  archive: (id: number) => request<DocumentDetail>(doc(id, "archive/"), { method: "POST" }),
  restore: (id: number) => request<DocumentDetail>(doc(id, "restore/"), { method: "POST" }),
  reprocess: (id: number) =>
    request<ProcessingJob>(doc(id, "reprocess/"), { method: "POST" }),
  versions: (id: number) => request<DocumentVersion[]>(doc(id, "versions/")),
  processing: (id: number) => request<ProcessingJob[]>(doc(id, "processing/")),
  content: (id: number) => request<DocumentContent>(doc(id, "content/")),
  classification: (id: number) =>
    request<ClassificationResult[]>(doc(id, "classification/")),
  extraction: (id: number) => request<ExtractionResult[]>(doc(id, "extraction/")),
  tables: (id: number) => request<DocumentTable[]>(doc(id, "tables/")),
  figures: (id: number) => request<DocumentFigure[]>(doc(id, "figures/")),
  source: (id: number, chunk: number) =>
    request<SourcePassage>(doc(id, `source/${query({ chunk })}`)),
  fileBlob: (id: number, version?: number, signal?: AbortSignal) =>
    fetchBlob(doc(id, `download/${query({ version })}`), signal),

  // --- search and chat -------------------------------------------------
  search: (input: SearchInput) =>
    request<SearchResponse>("/api/v1/search/", { method: "POST", body: input }),
  chat: (input: ChatInput) =>
    request<ChatResponse>("/api/v1/chat/", { method: "POST", body: input }),
  conversations: (page = 1) =>
    request<Paginated<ConversationSummary>>(`/api/v1/chat/conversations/${query({ page })}`),
  conversation: (id: number) => request<Conversation>(`/api/v1/chat/conversations/${id}/`),
  renameConversation: (id: number, title: string) =>
    request<Conversation>(`/api/v1/chat/conversations/${id}/`, {
      method: "PATCH",
      body: { title },
    }),
  deleteConversation: (id: number) =>
    request<void>(`/api/v1/chat/conversations/${id}/`, { method: "DELETE" }),
  feedback: (messageId: number, rating: FeedbackRating, comment = "") =>
    request<unknown>(`/api/v1/messages/${messageId}/feedback/`, {
      method: "POST",
      body: { rating, comment },
    }),

  // --- administration --------------------------------------------------
  adminUsers: (params: Record<string, string | number | undefined> = {}) =>
    request<Paginated<AdminUser>>(`/api/v1/admin/users/${query(params)}`),
  adminCreateUser: (body: Partial<AdminUser> & { password: string }) =>
    request<AdminUser>("/api/v1/admin/users/", { method: "POST", body }),
  adminUpdateUser: (id: number, body: Partial<AdminUser>) =>
    request<AdminUser>(`/api/v1/admin/users/${id}/`, { method: "PATCH", body }),
  adminSetPassword: (id: number, password: string) =>
    request<void>(`/api/v1/admin/users/${id}/set-password/`, {
      method: "POST",
      body: { password },
    }),
  adminRoles: () => request<RoleOption[]>("/api/v1/admin/users/roles/"),
  adminDepartments: (page = 1) =>
    request<Paginated<AdminDepartment>>(`/api/v1/admin/departments/${query({ page })}`),
  adminCreateDepartment: (body: Partial<AdminDepartment>) =>
    request<AdminDepartment>("/api/v1/admin/departments/", { method: "POST", body }),
  adminUpdateDepartment: (id: number, body: Partial<AdminDepartment>) =>
    request<AdminDepartment>(`/api/v1/admin/departments/${id}/`, { method: "PATCH", body }),
  adminAudit: (params: Record<string, string | number | undefined> = {}) =>
    request<Paginated<AuditEntry>>(`/api/v1/admin/audit/${query(params)}`),
  adminJobs: (params: Record<string, string | number | undefined> = {}) =>
    request<Paginated<OperationsJob>>(`/api/v1/admin/jobs/${query(params)}`),
  adminRetryJob: (id: number) =>
    request<OperationsJob>(`/api/v1/admin/jobs/${id}/retry/`, { method: "POST" }),
  adminSystem: () => request<SystemStatus>("/api/v1/admin/system/"),
};
