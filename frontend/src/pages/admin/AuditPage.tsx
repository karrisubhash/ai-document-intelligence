import { useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../../api/client";
import { useResource } from "../../api/hooks";
import { Icon } from "../../components/icons";
import {
  Avatar,
  Badge,
  EmptyState,
  ErrorState,
  PageHeader,
  Pagination,
  Skeleton,
  useDocumentTitle,
} from "../../components/ui";
import { formatDateTime } from "../../lib/format";
import { AUDIT_ACTION_LABEL } from "../../lib/labels";

function describeMetadata(metadata: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const [key, value] of Object.entries(metadata)) {
    if (value === null || value === undefined || value === "") continue;
    const text =
      typeof value === "object" ? JSON.stringify(value) : String(value);
    parts.push(`${key.replace(/_/g, " ")}: ${text}`);
  }
  return parts.join(" · ");
}

export default function AuditPage() {
  useDocumentTitle("Audit log");
  const [page, setPage] = useState(1);
  const [action, setAction] = useState("");
  const [result, setResult] = useState("");
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");

  // The date pickers choose LOCAL calendar days, and the rows are shown in
  // local time. Sent as bare dates they were read as UTC days, so "today"
  // was offset by the browser's distance from UTC (five and a half hours
  // in India). Sent as the local day's first and last instants instead.
  const entries = useResource(
    () =>
      api.adminAudit({
        page,
        action,
        result,
        since: since ? new Date(`${since}T00:00:00`).toISOString() : "",
        until: until ? new Date(`${until}T23:59:59.999`).toISOString() : "",
      }),
    [page, action, result, since, until],
  );
  const reset = (setter: (value: string) => void) => (value: string) => {
    setter(value);
    setPage(1);
  };

  return (
    <div className="page">
      <PageHeader
        title="Audit log"
        description="Who did what, and whether it was allowed. Queries and question text are never recorded — only that a search or question happened."
      />
      <div className="toolbar">
        <select
          className="input"
          aria-label="Action"
          value={action}
          onChange={(event) => reset(setAction)(event.target.value)}
        >
          <option value="">Every action</option>
          {Object.entries(AUDIT_ACTION_LABEL).map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
        <select
          className="input"
          aria-label="Result"
          value={result}
          onChange={(event) => reset(setResult)(event.target.value)}
        >
          <option value="">Allowed and denied</option>
          <option value="ALLOWED">Allowed</option>
          <option value="DENIED">Denied</option>
        </select>
        <label className="inline-field">
          From
          <input
            type="date"
            className="input"
            value={since}
            onChange={(event) => reset(setSince)(event.target.value)}
          />
        </label>
        <label className="inline-field">
          To
          <input
            type="date"
            className="input"
            value={until}
            onChange={(event) => reset(setUntil)(event.target.value)}
          />
        </label>
      </div>

      {entries.error && !entries.data ? (
        <ErrorState error={entries.error} onRetry={entries.reload} />
      ) : entries.loading && !entries.data ? (
        <div className="card card-body">
          <Skeleton lines={8} />
        </div>
      ) : !entries.data?.results.length ? (
        <div className="card">
          <EmptyState title="No matching audit entries" icon="scroll" />
        </div>
      ) : (
        <div className="table-wrap">
          <table className="table table-compact table-stack">
            <thead>
              <tr>
                <th scope="col">When</th>
                <th scope="col">Who</th>
                <th scope="col">Action</th>
                <th scope="col">Result</th>
                <th scope="col">Document</th>
                <th scope="col">Detail</th>
              </tr>
            </thead>
            <tbody>
              {entries.data.results.map((entry) => (
                <tr key={entry.id}>
                  <td className="nowrap stack-head">{formatDateTime(entry.created_at)}</td>
                  <td data-label="Who">
                    {entry.username ? (
                      <span className="cell-person">
                        <Avatar name={entry.username} size="sm" />
                        {entry.username}
                      </span>
                    ) : (
                      <span className="cell-person">
                        <span className="avatar avatar-sm avatar-unknown" aria-hidden="true">
                          <Icon name="user" size={12} />
                        </span>
                        <span className="muted">unknown</span>
                      </span>
                    )}
                  </td>
                  <td data-label="Action">{AUDIT_ACTION_LABEL[entry.action] ?? entry.action}</td>
                  <td data-label="Result">
                    <Badge
                      tone={entry.result === "ALLOWED" ? "success" : "danger"}
                      icon={entry.result === "ALLOWED" ? "check" : "x"}
                    >
                      {entry.result === "ALLOWED" ? "Allowed" : "Denied"}
                    </Badge>
                  </td>
                  <td data-label="Document">
                    {entry.document_withheld ? (
                      <span className="muted">Another department&rsquo;s document</span>
                    ) : entry.document ? (
                      <Link to={`/documents/${entry.document}`} className="link">
                        {entry.document_title || `Document #${entry.document}`}
                      </Link>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td className="small muted" data-label="Detail">
                    {describeMetadata(entry.metadata)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {entries.data ? (
        <Pagination page={page} count={entries.data.count} onPage={setPage} />
      ) : null}
    </div>
  );
}
