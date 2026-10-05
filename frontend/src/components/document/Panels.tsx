import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../../api/client";
import { usePolling, useResource } from "../../api/hooks";
import type { DocumentDetail, ExtractedField, ProcessingJob } from "../../api/types";
import { formatDateTime, formatDuration, formatPercent, formatRelative } from "../../lib/format";
import { extensionOf, isPreviewable } from "../../lib/files";
import { EVENT_TONE, STAGE_LABEL, humanize } from "../../lib/labels";
import PageText from "../PageText";
import { Alert, Badge, EmptyState, ErrorState, Skeleton, Spinner } from "../ui";

// --- Text -------------------------------------------------------------------

export function ContentPanel({
  document,
  page,
  start,
  end,
  onPage,
}: {
  document: DocumentDetail;
  page: number;
  start: number | null;
  end: number | null;
  onPage: (page: number) => void;
}) {
  const content = useResource(() => api.content(document.id), [document.id, document.status]);

  if (content.loading && !content.data) return <Skeleton lines={8} />;
  if (content.status === 404) {
    return (
      <EmptyState title="No text yet" icon="⏳">
        {document.status === "FAILED" || document.status === "QUARANTINED"
          ? "Processing did not reach the text stage. The Processing tab says why."
          : "Text appears here once processing has read the document."}
      </EmptyState>
    );
  }
  if (content.error || !content.data) {
    return <ErrorState error={content.error} onRetry={content.reload} />;
  }

  const pages = content.data.pages;
  const current = pages.find((item) => item.page_number === page) ?? pages[0];
  if (!current) return <EmptyState title="This document has no pages" />;
  const highlight = current.page_number === page;

  return (
    <div className="content-panel">
      <div className="content-toolbar">
        <label className="inline-field">
          Page
          <select
            className="input input-sm"
            value={current.page_number}
            onChange={(event) => onPage(Number(event.target.value))}
          >
            {pages.map((item) => (
              <option key={item.page_number} value={item.page_number}>
                {item.page_number} of {pages.length}
              </option>
            ))}
          </select>
        </label>
        <div className="button-row">
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            disabled={current.page_number <= pages[0].page_number}
            onClick={() => onPage(current.page_number - 1)}
          >
            Previous page
          </button>
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            disabled={current.page_number >= pages[pages.length - 1].page_number}
            onClick={() => onPage(current.page_number + 1)}
          >
            Next page
          </button>
        </div>
        <Badge
          tone={current.extraction_method === "OCR" ? "warning" : "neutral"}
          title="How this page's text was obtained"
        >
          {current.extraction_method === "OCR" ? "Read by OCR" : humanize(current.extraction_method)}
        </Badge>
      </div>
      {current.extraction_method === "OCR" ? (
        <p className="muted small">
          This page was scanned, so its text was recognised by OCR and may contain
          recognition errors. Check important figures against the original.
        </p>
      ) : null}
      <PageText
        text={current.text}
        start={highlight ? start : null}
        end={highlight ? end : null}
        label={`Page ${current.page_number} text`}
      />
    </div>
  );
}

// --- Processing -------------------------------------------------------------------

function JobCard({ job, latest }: { job: ProcessingJob; latest: boolean }) {
  const [open, setOpen] = useState(latest);
  const tone =
    job.status === "COMPLETED"
      ? "success"
      : job.status === "FAILED"
        ? "danger"
        : "info";
  const events = [...job.events].sort(
    (a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime(),
  );
  return (
    <details className="job" open={open} onToggle={(event) => setOpen((event.target as HTMLDetailsElement).open)}>
      <summary className="job-summary">
        <Badge tone={tone}>{humanize(job.status)}</Badge>
        <span className="job-title">
          Run #{job.id}
          {job.attempt > 1 ? ` · attempt ${job.attempt}` : ""}
        </span>
        <span className="muted small">{formatRelative(job.created_at)}</span>
      </summary>
      {job.error_code ? (
        <Alert tone={job.error_code === "MALWARE_DETECTED" ? "warning" : "danger"} title={humanize(job.error_code)}>
          {job.error_message || "No further detail was recorded."}
        </Alert>
      ) : null}
      <ol className="timeline">
        {events.map((event) => (
          <li key={event.id} className={`timeline-item tone-${EVENT_TONE[event.status] ?? "neutral"}`}>
            <div className="timeline-head">
              <span className="timeline-stage">{STAGE_LABEL[event.stage] ?? event.stage}</span>
              <Badge tone={EVENT_TONE[event.status] ?? "neutral"}>{humanize(event.status)}</Badge>
              {typeof event.metadata?.duration_seconds === "number" ? (
                <span className="muted small">
                  {formatDuration((event.metadata.duration_seconds as number) * 1000)}
                </span>
              ) : null}
            </div>
            {event.message ? <p className="timeline-message">{event.message}</p> : null}
          </li>
        ))}
      </ol>
    </details>
  );
}

export function ProcessingPanel({
  document,
  onSettled,
}: {
  document: DocumentDetail;
  onSettled: () => void;
}) {
  const jobs = useResource(() => api.processing(document.id), [document.id]);
  const active = (jobs.data ?? []).some(
    (job) => job.status === "QUEUED" || job.status === "PROCESSING",
  );
  usePolling(jobs.reload, active, 2500);

  // When the last active run finishes, the document's own status changed
  // too - tell the page so its header is not left saying "Processing".
  const [wasActive, setWasActive] = useState(false);
  useEffect(() => {
    if (active) setWasActive(true);
    else if (wasActive) {
      setWasActive(false);
      onSettled();
    }
  }, [active, wasActive, onSettled]);

  if (jobs.loading && !jobs.data) return <Skeleton lines={6} />;
  if (jobs.error) return <ErrorState error={jobs.error} onRetry={jobs.reload} />;
  if (!jobs.data?.length) return <EmptyState title="No processing runs yet" />;

  return (
    <div className="stack">
      {active ? <Spinner label="Processing — this page updates as each stage finishes." /> : null}
      {jobs.data.map((job, index) => (
        <JobCard key={job.id} job={job} latest={index === 0} />
      ))}
    </div>
  );
}

// --- Fields -------------------------------------------------------------------------

const FIELD_TONE = {
  FOUND: "success",
  NOT_FOUND: "muted",
  INVALID: "warning",
  CONFLICTED: "danger",
} as const;

function fieldLink(documentId: number, field: ExtractedField) {
  if (field.page_number === null || field.character_start === null) return null;
  return `/documents/${documentId}?tab=text&page=${field.page_number}&start=${field.character_start}&end=${field.character_end}`;
}

export function FieldsPanel({ document }: { document: DocumentDetail }) {
  const results = useResource(() => api.extraction(document.id), [document.id, document.status]);

  if (results.loading && !results.data) return <Skeleton lines={6} />;
  if (results.error && results.status !== 404) {
    return <ErrorState error={results.error} onRetry={results.reload} />;
  }
  const latest = results.data?.[0];
  if (!latest) {
    return (
      <EmptyState title="No fields extracted" icon="🔎">
        Field extraction has not run on this version, or is not enabled on this
        installation. When it runs, every declared field is listed here — including the
        ones it could not find.
      </EmptyState>
    );
  }

  return (
    <div className="stack">
      <p className="muted small">
        {latest.found_count} of {latest.field_count} fields found by {latest.model_name}{" "}
        {latest.model_version} using the {latest.schema_name} schema. Every value links to
        the exact place it was read from.
      </p>
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Field</th>
              <th scope="col">Value</th>
              <th scope="col">Status</th>
              <th scope="col">Confidence</th>
              <th scope="col">Source</th>
            </tr>
          </thead>
          <tbody>
            {latest.extracted_fields.map((field) => {
              const link = fieldLink(document.id, field);
              return (
                <tr key={field.id}>
                  <th scope="row">{humanize(field.field_name)}</th>
                  <td>{field.value || <span className="muted">—</span>}</td>
                  <td>
                    <Badge tone={FIELD_TONE[field.status]}>{humanize(field.status)}</Badge>
                    {field.status === "CONFLICTED" ? (
                      <span className="table-sub">
                        The document states different values; none was chosen.
                      </span>
                    ) : null}
                  </td>
                  <td>{field.status === "FOUND" ? formatPercent(field.confidence) : "—"}</td>
                  <td>
                    {link ? (
                      <Link to={link} className="link">
                        Page {field.page_number}
                      </Link>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// --- Tables ---------------------------------------------------------------------------

export function TablesPanel({ document }: { document: DocumentDetail }) {
  const tables = useResource(() => api.tables(document.id), [document.id, document.status]);

  if (tables.loading && !tables.data) return <Skeleton lines={6} />;
  if (tables.error) return <ErrorState error={tables.error} onRetry={tables.reload} />;
  if (!tables.data?.length) {
    return <EmptyState title="No tables detected" icon="▦" />;
  }

  return (
    <div className="stack">
      {tables.data.map((table) => (
        <section key={table.id} className="card">
          <div className="card-header">
            <h3 className="card-title">
              Table {table.table_index + 1}{" "}
              <span className="muted small">
                · {table.row_count} rows × {table.column_count} columns
              </span>
            </h3>
            <Link
              className="link"
              to={`/documents/${document.id}?tab=text&page=${table.page_number}&start=${table.character_start}&end=${table.character_end}`}
            >
              Page {table.page_number}
            </Link>
          </div>
          <div className="table-wrap">
            <table className="table table-compact">
              {table.header.length ? (
                <thead>
                  <tr>
                    {table.header.map((cell, index) => (
                      <th key={index} scope="col">
                        {cell}
                      </th>
                    ))}
                  </tr>
                </thead>
              ) : null}
              <tbody>
                {table.rows.slice(table.header.length ? 1 : 0).map((row, rowIndex) => (
                  <tr key={rowIndex}>
                    {row.map((cell, cellIndex) => (
                      <td key={cellIndex}>{cell}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ))}
    </div>
  );
}

// --- Figures --------------------------------------------------------------------------

export function FiguresPanel({ document }: { document: DocumentDetail }) {
  const figures = useResource(() => api.figures(document.id), [document.id, document.status]);

  if (figures.loading && !figures.data) return <Skeleton lines={4} />;
  if (figures.error) return <ErrorState error={figures.error} onRetry={figures.reload} />;
  if (!figures.data?.length) return <EmptyState title="No figures found" icon="🖼" />;

  return (
    <div className="stack">
      <p className="muted small">
        Figures are located, not read: any text inside them was extracted by OCR, but charts
        and diagrams themselves are not interpreted. Questions about them may be answered
        with a pointer to the page instead.
      </p>
      <ul className="row-list">
        {figures.data.map((figure) => (
          <li key={figure.id} className="row-item">
            <div className="row-link">
              <span className="row-title">
                Figure {figure.figure_index + 1} on page {figure.page_number}
              </span>
              <span className="row-meta">
                {figure.width && figure.height ? `${figure.width} × ${figure.height} px · ` : ""}
                {figure.description || "Not described"}
              </span>
            </div>
            <Link className="link" to={`/documents/${document.id}?tab=text&page=${figure.page_number}`}>
              Open page
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}

// --- Preview ---------------------------------------------------------------------------

export function PreviewPanel({ document }: { document: DocumentDetail }) {
  const version = document.current_version;
  const kind = version ? isPreviewable(version.mime_type, version.file_name) : null;
  const [url, setUrl] = useState<string | null>(null);
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);

  // Keyed on primitives, never on the version OBJECT. The page polls the
  // document every few seconds while it processes, and every poll returns a
  // new current_version object; with that as a dependency the preview
  // re-downloaded the whole file - and wrote a download audit row - on every
  // poll, dropping back to a skeleton each time.
  const versionId = version?.id;
  const fileName = version?.file_name ?? "";
  const quarantined = document.status === "QUARANTINED";

  useEffect(() => {
    if (!versionId || !kind || quarantined) return;
    let revoked: string | null = null;
    let cancelled = false;
    const controller = new AbortController();
    setUrl(null);
    setText(null);
    setError(null);
    api
      .fileBlob(document.id, undefined, controller.signal)
      .then(async (blob) => {
        if (cancelled) return;
        if (kind === "text") {
          setText(await blob.text());
          return;
        }
        // Re-typed from the file EXTENSION the server validated, never from
        // the response or the MIME type the uploader's browser declared -
        // that one is client input, and a declared "text/html" must not
        // turn a stored file into a page that runs.
        const typed = new Blob([blob], {
          type:
            kind === "pdf"
              ? "application/pdf"
              : extensionOf(fileName) === ".png"
                ? "image/png"
                : "image/jpeg",
        });
        revoked = URL.createObjectURL(typed);
        setUrl(revoked);
      })
      .catch((caught) => {
        if (!cancelled) setError(caught);
      });
    return () => {
      cancelled = true;
      controller.abort();
      if (revoked) URL.revokeObjectURL(revoked);
    };
  }, [document.id, versionId, fileName, kind, quarantined]);

  if (!version) return <EmptyState title="No file" />;
  if (document.status === "QUARANTINED") {
    return (
      <Alert tone="warning" title="Preview disabled">
        This file was quarantined by the security scan and is not opened in the browser.
        An administrator can review the scan findings on the Processing tab.
      </Alert>
    );
  }
  if (!kind) {
    return (
      <EmptyState title="No preview for this file type" icon="📎">
        Word documents are not previewed in the browser. The Text tab shows everything that
        was extracted from it, and Download opens the original.
      </EmptyState>
    );
  }
  if (error) return <ErrorState error={error} />;
  if (kind === "text") {
    return text === null ? <Skeleton lines={8} /> : <PageText text={text} label="File contents" />;
  }
  if (!url) return <Skeleton lines={8} />;
  if (kind === "image") {
    return (
      <div className="preview">
        <img src={url} alt={`Scanned page of ${document.title}`} className="preview-image" />
      </div>
    );
  }
  return (
    <div className="preview">
      <iframe title={`Preview of ${document.title}`} src={url} className="preview-frame" />
      <p className="muted small">
        Uploaded {formatDateTime(version.created_at)} · version {version.version_number}
      </p>
    </div>
  );
}
