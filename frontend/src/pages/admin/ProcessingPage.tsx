import { useState } from "react";
import { Link } from "react-router-dom";

import { ApiError, api } from "../../api/client";
import { usePolling, useResource } from "../../api/hooks";
import type { OperationsJob } from "../../api/types";
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  PageHeader,
  Pagination,
  Skeleton,
  errorMessage,
  useDocumentTitle,
  useToast,
} from "../../components/ui";
import { formatRelative } from "../../lib/format";
import { STAGE_LABEL, humanize } from "../../lib/labels";

const VIEWS: [string, string, Record<string, string>][] = [
  ["failed", "Failed", { status: "FAILED" }],
  ["running", "Running", { status: "PROCESSING" }],
  ["queued", "Queued", { status: "QUEUED" }],
  ["stalled", "Stalled", { stalled: "true" }],
  ["all", "All", {}],
];

function statusTone(job: OperationsJob) {
  if (job.is_stalled) return "warning" as const;
  if (job.status === "FAILED") return "danger" as const;
  if (job.status === "COMPLETED") return "success" as const;
  return "info" as const;
}

export default function ProcessingPage() {
  useDocumentTitle("Processing");
  const toast = useToast();
  const [view, setView] = useState("failed");
  const [page, setPage] = useState(1);
  const [retrying, setRetrying] = useState<number | null>(null);
  const filters = VIEWS.find(([id]) => id === view)?.[2] ?? {};

  const jobs = useResource(() => api.adminJobs({ page, ...filters }), [page, view]);
  usePolling(jobs.reload, view === "running" || view === "queued", 5000);

  const retry = async (job: OperationsJob) => {
    setRetrying(job.id);
    try {
      await api.adminRetryJob(job.id);
      toast(`Processing restarted for “${job.document_title}”.`);
      jobs.reload();
    } catch (caught) {
      // A 409 has two causes - a run already queued, or an ARCHIVED
      // document (restore it first) - and the server's own message names
      // which. Every 409 used to be reported as the first.
      const detail =
        caught instanceof ApiError &&
        typeof (caught.payload as { detail?: unknown } | null)?.detail === "string"
          ? (caught.payload as { detail: string }).detail
          : "";
      toast(
        caught instanceof ApiError && caught.status === 409
          ? detail || "A run for this document is already queued or in progress."
          : errorMessage(caught),
        "danger",
      );
    } finally {
      setRetrying(null);
    }
  };

  return (
    <div className="page">
      <PageHeader
        title="Processing"
        description="Pipeline runs across the documents you administer. A stalled run stopped reporting progress — usually a worker that was restarted mid-job."
      />
      <div className="segmented" role="group" aria-label="Show">
        {VIEWS.map(([id, label]) => (
          <button
            key={id}
            type="button"
            className={`segment${view === id ? " active" : ""}`}
            aria-pressed={view === id}
            onClick={() => {
              setView(id);
              setPage(1);
            }}
          >
            {label}
          </button>
        ))}
      </div>

      {jobs.error && !jobs.data ? (
        <ErrorState error={jobs.error} onRetry={jobs.reload} />
      ) : jobs.loading && !jobs.data ? (
        <Skeleton lines={6} />
      ) : !jobs.data?.results.length ? (
        <EmptyState title="Nothing here" icon="✓">
          {view === "failed" ? "No failed runs. Everything processed cleanly." : "No runs in this view."}
        </EmptyState>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Document</th>
                <th scope="col">Status</th>
                <th scope="col">Stage</th>
                <th scope="col">Problem</th>
                <th scope="col">Updated</th>
                <th scope="col">
                  <span className="visually-hidden">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {jobs.data.results.map((job) => (
                <tr key={job.id}>
                  <td>
                    <Link to={`/documents/${job.document_id}?tab=processing`} className="table-title">
                      {job.document_title}
                    </Link>
                    <span className="table-sub">
                      Run #{job.id} · v{job.version_number}
                      {!job.is_current_version ? " (superseded)" : ""}
                      {job.department ? ` · ${job.department}` : ""}
                    </span>
                  </td>
                  <td>
                    <Badge tone={statusTone(job)}>
                      {job.is_stalled ? "Stalled" : humanize(job.status)}
                    </Badge>
                  </td>
                  <td>{job.current_stage ? STAGE_LABEL[job.current_stage] : "—"}</td>
                  <td className="small">
                    {job.error_code ? (
                      <>
                        <strong>{humanize(job.error_code)}</strong>
                        {job.error_message ? (
                          <span className="table-sub">{job.error_message.slice(0, 160)}</span>
                        ) : null}
                      </>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td>{formatRelative(job.updated_at)}</td>
                  <td>
                    {(job.status === "FAILED" || job.is_stalled) && job.is_current_version ? (
                      <Button
                        size="sm"
                        loading={retrying === job.id}
                        onClick={() => retry(job)}
                        disabled={job.error_code === "MALWARE_DETECTED"}
                        title={
                          job.error_code === "MALWARE_DETECTED"
                            ? "Quarantined files are not retried"
                            : undefined
                        }
                      >
                        Retry
                      </Button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {jobs.data ? <Pagination page={page} count={jobs.data.count} onPage={setPage} /> : null}
    </div>
  );
}
