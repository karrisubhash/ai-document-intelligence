import { useState } from "react";

import { thumbnailUrl } from "../../api/insights";
import type { DocumentSummary } from "../../api/types";
import { extensionOf } from "../../lib/files";
import { formatBytes, formatPercent } from "../../lib/format";
import { STAGE_LABEL, TYPE_LABEL } from "../../lib/labels";
import "../../styles/insights.css";

/*
 * The library's per-row pieces that come from document intelligence: the
 * first-page picture, the detected type, and where an in-flight run is.
 * Kept out of DocumentsPage so the page itself changes by a few lines.
 */

/**
 * The first page, small - or, for a file with no page image (Word, text),
 * a little page of lines with its format on it. Decorative: the title next
 * to it is the link and the name.
 */
export function DocumentThumb({ row }: { row: DocumentSummary }) {
  const [broken, setBroken] = useState(false);
  const version = row.current_version;
  if (row.has_thumbnail && version && !broken) {
    return (
      <img
        className="doc-thumb"
        src={thumbnailUrl(row.id, version.id)}
        alt=""
        loading="lazy"
        decoding="async"
        width={34}
        height={44}
        onError={() => setBroken(true)}
      />
    );
  }
  const extension = extensionOf(version?.file_name ?? "").toUpperCase().slice(0, 4);
  return (
    <span className="doc-thumb doc-textcard" aria-hidden="true">
      <span />
      <span />
      <span />
      <span />
      {extension ? <b>{extension}</b> : null}
    </span>
  );
}

/** "report.pdf · v2 · 12 pages · 1.2 MB" */
export function fileLine(row: DocumentSummary): string {
  const version = row.current_version;
  if (!version) return "No file";
  const parts = [version.file_name, `v${version.version_number}`];
  if (row.page_count) parts.push(`${row.page_count} ${row.page_count === 1 ? "page" : "pages"}`);
  parts.push(formatBytes(version.file_size));
  return parts.join(" · ");
}

/** The type, marked when the detector chose it rather than a person. */
export function TypeCell({ row }: { row: DocumentSummary }) {
  const auto = row.document_type_source === "auto";
  return (
    <>
      {TYPE_LABEL[row.document_type]}
      {auto ? (
        <span
          className="type-auto"
          title={
            row.type_confidence != null
              ? `Detected automatically (${formatPercent(row.type_confidence)})`
              : "Detected automatically"
          }
        >
          auto
        </span>
      ) : null}
    </>
  );
}

/** Where an in-flight run is: "Reading text", "OCR", ... */
export function StageNote({ row }: { row: DocumentSummary }) {
  if (!row.current_stage) return null;
  return <span className="live-stage">{STAGE_LABEL[row.current_stage] ?? row.current_stage}</span>;
}
