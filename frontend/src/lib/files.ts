/**
 * Client-side upload checks.
 *
 * A courtesy, not a control: the server enforces every one of these again
 * (documents/uploads.py) and a hand-crafted request skips this file
 * entirely. What this buys is telling someone their 80 MB scan is too big
 * BEFORE they wait for it to upload.
 */

// Mirrors EXTENSION_TYPES in backend/documents/processing/stages/ingestion.py.
export const ACCEPTED_EXTENSIONS = [".pdf", ".docx", ".txt", ".png", ".jpg", ".jpeg"];

export const ACCEPT_ATTRIBUTE = ACCEPTED_EXTENSIONS.join(",");

// MAX_DOCUMENT_SIZE_BYTES in backend/config/settings.py.
export const MAX_UPLOAD_BYTES = 50 * 1024 * 1024;

export function extensionOf(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot >= 0 ? name.slice(dot).toLowerCase() : "";
}

export function validateFile(file: File | null | undefined): string | null {
  if (!file) return "Choose a file to upload.";
  if (file.size === 0) return "This file is empty.";
  if (file.size > MAX_UPLOAD_BYTES) {
    return `This file is ${(file.size / 1024 / 1024).toFixed(1)} MB; the limit is 50 MB.`;
  }
  if (!ACCEPTED_EXTENSIONS.includes(extensionOf(file.name))) {
    return `Unsupported file type. Upload one of: ${ACCEPTED_EXTENSIONS.join(", ")}.`;
  }
  return null;
}

/** A title from a filename: "exam_regulations-2024.pdf" -> "Exam regulations 2024". */
export function titleFromFilename(name: string): string {
  const base = name.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " ").trim();
  return base ? base.charAt(0).toUpperCase() + base.slice(1) : "";
}

export function isPreviewable(mime: string, name: string): "pdf" | "image" | "text" | null {
  const ext = extensionOf(name);
  if (mime === "application/pdf" || ext === ".pdf") return "pdf";
  if (mime.startsWith("image/") || [".png", ".jpg", ".jpeg"].includes(ext)) return "image";
  if (mime.startsWith("text/") || ext === ".txt") return "text";
  return null;
}

const FORMAT_LABEL: Record<string, string> = {
  "application/pdf": "PDF document",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Word document",
  "text/plain": "Plain text",
  "image/png": "PNG image",
  "image/jpeg": "JPEG image",
};

/** "application/pdf" -> "PDF document"; an unknown type is shown as it is. */
export function formatLabel(mime: string): string {
  return FORMAT_LABEL[mime] ?? (mime || "—");
}
