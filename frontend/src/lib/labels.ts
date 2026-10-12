import type { DocumentStatus, DocumentType, Role, Stage } from "../api/types";

export type Tone = "neutral" | "info" | "success" | "warning" | "danger" | "muted";

export const STATUS_LABEL: Record<DocumentStatus, string> = {
  UPLOADED: "Uploaded",
  QUEUED: "Queued",
  PROCESSING: "Processing",
  COMPLETED: "Ready",
  FAILED: "Failed",
  QUARANTINED: "Quarantined",
};

export const STATUS_TONE: Record<DocumentStatus, Tone> = {
  UPLOADED: "neutral",
  QUEUED: "info",
  PROCESSING: "info",
  COMPLETED: "success",
  FAILED: "danger",
  QUARANTINED: "warning",
};

/** Statuses a document can still move out of on its own. */
export const IN_FLIGHT: DocumentStatus[] = ["UPLOADED", "QUEUED", "PROCESSING"];

export const TYPE_LABEL: Record<DocumentType, string> = {
  GENERAL: "General",
  REGULATION: "Regulation",
  POLICY: "Policy",
  SYLLABUS: "Syllabus",
  CIRCULAR: "Circular",
  FORM: "Form",
  OTHER: "Other",
};

export const ROLE_LABEL: Record<Role, string> = {
  SYSTEM_ADMIN: "System administrator",
  DEPARTMENT_ADMIN: "Department administrator",
  FACULTY: "Faculty",
  STAFF: "Staff",
  STUDENT: "Student",
};

export const STAGE_LABEL: Record<Stage, string> = {
  INGESTION: "Ingestion",
  SCANNING: "Security scan",
  VALIDATION: "Validation",
  TEXT_EXTRACTION: "Text extraction",
  OCR: "OCR",
  LAYOUT_ANALYSIS: "Layout analysis",
  CLASSIFICATION: "Classification",
  EXTRACTION: "Field extraction",
  INDEXING: "Indexing",
};

export const EVENT_TONE: Record<string, Tone> = {
  STARTED: "info",
  SUCCEEDED: "success",
  SKIPPED: "muted",
  FAILED: "danger",
};

export const AUDIT_ACTION_LABEL: Record<string, string> = {
  DOCUMENT_VIEW: "Viewed document",
  DOCUMENT_CREATE: "Uploaded document",
  DOCUMENT_UPDATE: "Edited document",
  DOCUMENT_DELETE: "Deleted document",
  DOCUMENT_DOWNLOAD: "Downloaded document",
  DOCUMENT_REPROCESS: "Reprocessed document",
  DOCUMENT_VERSION_UPLOAD: "Uploaded new version",
  DOCUMENT_ARCHIVE: "Archived document",
  DOCUMENT_RESTORE: "Restored document",
  SEARCH: "Searched",
  CHAT: "Asked a question",
  LOGIN: "Signed in",
  LOGIN_FAILED: "Failed sign-in",
  LOGOUT: "Signed out",
  PASSWORD_CHANGE: "Changed password",
  USER_CREATE: "Created account",
  USER_UPDATE: "Updated account",
  DEPARTMENT_CREATE: "Created department",
  DEPARTMENT_UPDATE: "Updated department",
  JOB_RETRY: "Retried processing",
};

/**
 * Plain-language explanations for why an answer was withheld. The backend's
 * reasons are precise and technical; a reader needs to know what to DO.
 */
export function explainAbstention(reason: string): string {
  const text = reason.toLowerCase();
  if (text.includes("no authorized documents")) {
    return "None of the documents you can access mention this. It may be in a document you do not have access to, or it may not be documented at all.";
  }
  if (text.includes("not relevant enough")) {
    return "Some documents share words with your question, but none of them actually addresses it. Try naming the policy, course or form you mean.";
  }
  if (text.includes("could not be grounded")) {
    return "A draft answer was produced but could not be verified against the source passages, so it was withheld rather than risk a wrong answer.";
  }
  if (text.includes("figures")) {
    return "The relevant pages contain charts or images this system cannot read. Open the pages listed below to check them yourself.";
  }
  if (text.includes("found nothing")) {
    return "The retrieved passages were read and none of them answers the question.";
  }
  if (text.includes("usable answer")) {
    return "Relevant passages were found, but the language model could not finish an answer this time: it was unavailable, declined, or ran out of room. Try again, or ask about one thing at a time.";
  }
  if (text.includes("parts of the question")) {
    return "The question was split into parts, and none of the parts could be answered from your documents.";
  }
  if (text.includes("empty question")) return "The question was empty.";
  return "There was not enough evidence in your documents to answer this reliably.";
}

export function humanize(value: string): string {
  return value
    .toLowerCase()
    .replace(/_/g, " ")
    .replace(/^\w/, (letter) => letter.toUpperCase());
}
