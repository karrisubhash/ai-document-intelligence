/**
 * Client-side upload checks.
 *
 * A courtesy, not a control: the server enforces every one of these again
 * (documents/uploads.py) and a hand-crafted request skips this file
 * entirely. What this buys is telling someone their 80 MB scan is too big
 * BEFORE they wait for it to upload.
 */

// Mirrors EXTENSION_TYPES in backend/documents/processing/stages/ingestion.py.
export const ACCEPTED_EXTENSIONS = [
  ".pdf",
  ".docx",
  ".xlsx",
  ".pptx",
  ".doc",
  ".xls",
  ".ppt",
  ".odt",
  ".ods",
  ".odp",
  ".rtf",
  ".html",
  ".htm",
  ".xhtml",
  ".csv",
  ".tsv",
  ".json",
  ".jsonl",
  ".ndjson",
  ".xml",
  ".txt",
  ".md",
  ".markdown",
  ".rst",
  ".log",
  ".text",
  ".yaml",
  ".yml",
  ".ini",
  ".cfg",
  ".conf",
  ".toml",
  ".tex",
  ".srt",
  ".vtt",
  ".sql",
  ".png",
  ".jpg",
  ".jpeg",
  ".tif",
  ".tiff",
  ".webp",
  ".bmp",
];

export const ACCEPT_ATTRIBUTE = ACCEPTED_EXTENSIONS.join(",");

/** The upload dialog also takes a .zip, which it opens in the browser (lib/zip.ts). */
export const BATCH_ACCEPT_ATTRIBUTE = [...ACCEPTED_EXTENSIONS, ".zip"].join(",");

// MAX_DOCUMENT_SIZE_BYTES in backend/config/settings.py.
export const MAX_UPLOAD_BYTES = 50 * 1024 * 1024;

export function extensionOf(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot >= 0 ? name.slice(dot).toLowerCase() : "";
}

export function validateFile(file: File | null | undefined): string | null {
  if (!file) return "Choose a file to upload.";
  return fileProblem(file.name, file.size);
}

/** What is wrong with a file of this name and size, or null - for a file not yet read. */
export function fileProblem(name: string, size: number): string | null {
  if (size === 0) return "This file is empty.";
  if (size > MAX_UPLOAD_BYTES) {
    return `This file is ${(size / 1024 / 1024).toFixed(1)} MB; the limit is 50 MB.`;
  }
  if (!ACCEPTED_EXTENSIONS.includes(extensionOf(name))) {
    return "Unsupported file type. Upload a PDF, Word, Excel, PowerPoint (old or new), OpenDocument, RTF, HTML, text, Markdown, CSV, JSON, XML or image file.";
  }
  return null;
}

/**
 * The type a browser gives a picked file, for a file taken out of a zip,
 * which has none. The server stores it as the version's mime_type; what
 * the file really is, it decides by sniffing the content.
 */
export const MIME_BY_EXTENSION: Record<string, string> = {
  ".pdf": "application/pdf",
  ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  ".doc": "application/msword",
  ".xls": "application/vnd.ms-excel",
  ".ppt": "application/vnd.ms-powerpoint",
  ".odt": "application/vnd.oasis.opendocument.text",
  ".ods": "application/vnd.oasis.opendocument.spreadsheet",
  ".odp": "application/vnd.oasis.opendocument.presentation",
  ".rtf": "application/rtf",
  ".html": "text/html",
  ".htm": "text/html",
  ".xhtml": "application/xhtml+xml",
  ".json": "application/json",
  ".jsonl": "application/x-ndjson",
  ".ndjson": "application/x-ndjson",
  ".xml": "application/xml",
  ".md": "text/markdown",
  ".markdown": "text/markdown",
  ".csv": "text/csv",
  ".tsv": "text/tab-separated-values",
  ".txt": "text/plain",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".tif": "image/tiff",
  ".tiff": "image/tiff",
  ".webp": "image/webp",
  ".bmp": "image/bmp",
};

/** A title from a filename: "exam_regulations-2024.pdf" -> "Exam regulations 2024". */
export function titleFromFilename(name: string): string {
  const base = name.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " ").trim();
  return base ? base.charAt(0).toUpperCase() + base.slice(1) : "";
}

export function isPreviewable(mime: string, name: string): "pdf" | "image" | "text" | null {
  const ext = extensionOf(name);
  if (mime === "application/pdf" || ext === ".pdf") return "pdf";
  // A browser cannot draw a TIFF (only Safari can), so it has no preview.
  if (ext === ".tif" || ext === ".tiff" || mime === "image/tiff") return null;
  if (mime.startsWith("image/") || [".png", ".jpg", ".jpeg", ".webp", ".bmp"].includes(ext)) {
    return "image";
  }
  // A delimited file is text: its rows are readable as they are.
  if (mime.startsWith("text/") || [".txt", ".csv", ".tsv"].includes(ext)) return "text";
  return null;
}

const FORMAT_LABEL: Record<string, string> = {
  "application/pdf": "PDF document",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Word document",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "Excel workbook",
  "application/vnd.openxmlformats-officedocument.presentationml.presentation": "PowerPoint deck",
  "text/csv": "CSV file",
  "text/tab-separated-values": "TSV file",
  // What Windows browsers report for a .csv when Excel is installed.
  "application/vnd.ms-excel": "Spreadsheet",
  "text/plain": "Plain text",
  "image/png": "PNG image",
  "image/jpeg": "JPEG image",
  "image/tiff": "TIFF image",
  "image/webp": "WebP image",
  "image/bmp": "BMP image",
};

/** "application/pdf" -> "PDF document"; an unknown type is shown as it is. */
export function formatLabel(mime: string): string {
  return FORMAT_LABEL[mime] ?? (mime || "—");
}
