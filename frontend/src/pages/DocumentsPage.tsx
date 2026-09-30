import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { api } from "../api/client";
import { useDebouncedValue, usePolling, useResource } from "../api/hooks";
import { DOCUMENT_STATUSES, DOCUMENT_TYPES } from "../api/types";
import UploadDialog from "../components/UploadDialog";
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  PageHeader,
  Pagination,
  Skeleton,
  StatusBadge,
  useDocumentTitle,
} from "../components/ui";
import { formatBytes, formatRelative } from "../lib/format";
import { IN_FLIGHT, STATUS_LABEL, TYPE_LABEL } from "../lib/labels";

const SORTS: [string, string][] = [
  ["-updated_at", "Recently updated"],
  ["-created_at", "Newest first"],
  ["created_at", "Oldest first"],
  ["title", "Title A–Z"],
  ["-title", "Title Z–A"],
  ["status", "Status"],
];

export default function DocumentsPage() {
  useDocumentTitle("Documents");
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [uploading, setUploading] = useState(false);

  const page = Number(params.get("page") || "1") || 1;
  const status = params.get("status") ?? "";
  const documentType = params.get("type") ?? "";
  const department = params.get("department") ?? "";
  const ordering = params.get("sort") ?? "-updated_at";
  const archived = (params.get("archived") as "false" | "true" | "all" | null) ?? "false";
  const mine = params.get("mine") === "1";

  const [searchInput, setSearchInput] = useState(params.get("q") ?? "");
  const search = useDebouncedValue(searchInput.trim(), 350);

  const update = (changes: Record<string, string>) => {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(changes)) {
      if (value) next.set(key, value);
      else next.delete(key);
    }
    // Any change to what is being listed starts again at page one.
    if (!("page" in changes)) next.delete("page");
    setParams(next, { replace: true });
  };

  useEffect(() => {
    if (search !== (params.get("q") ?? "")) update({ q: search });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search]);

  const departments = useResource(() => api.departments(), []);
  const documents = useResource(
    () =>
      api.documents({
        page,
        search,
        status,
        document_type: documentType,
        department,
        ordering,
        archived,
        owner: mine ? "me" : undefined,
      }),
    [page, search, status, documentType, department, ordering, archived, mine],
  );

  const rows = documents.data?.results ?? [];
  const anyInFlight = rows.some((row) => IN_FLIGHT.includes(row.status));
  usePolling(documents.reload, anyInFlight);

  const filtered = Boolean(search || status || documentType || department || mine);

  return (
    <div className="page">
      <PageHeader
        title="Documents"
        description="Everything you have access to. Uploads are scanned, read and indexed in the background."
        actions={
          <Button variant="primary" onClick={() => setUploading(true)}>
            Upload document
          </Button>
        }
      />

      <div className="toolbar" role="search" aria-label="Filter documents">
        <input
          type="search"
          className="input toolbar-search"
          placeholder="Search titles and file names…"
          aria-label="Search titles and file names"
          value={searchInput}
          onChange={(event) => setSearchInput(event.target.value)}
        />
        <select
          className="input"
          aria-label="Status"
          value={status}
          onChange={(event) => update({ status: event.target.value })}
        >
          <option value="">Any status</option>
          {DOCUMENT_STATUSES.map((value) => (
            <option key={value} value={value}>
              {STATUS_LABEL[value]}
            </option>
          ))}
        </select>
        <select
          className="input"
          aria-label="Type"
          value={documentType}
          onChange={(event) => update({ type: event.target.value })}
        >
          <option value="">Any type</option>
          {DOCUMENT_TYPES.map((value) => (
            <option key={value} value={value}>
              {TYPE_LABEL[value]}
            </option>
          ))}
        </select>
        <select
          className="input"
          aria-label="Department"
          value={department}
          onChange={(event) => update({ department: event.target.value })}
        >
          <option value="">Any department</option>
          {(departments.data ?? []).map((item) => (
            <option key={item.id} value={item.name}>
              {item.name}
            </option>
          ))}
        </select>
        <select
          className="input"
          aria-label="Sort by"
          value={ordering}
          onChange={(event) => update({ sort: event.target.value })}
        >
          {SORTS.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
        <div className="segmented" role="group" aria-label="Archive">
          {(
            [
              ["false", "Active"],
              ["true", "Archived"],
              ["all", "All"],
            ] as const
          ).map(([value, label]) => (
            <button
              key={value}
              type="button"
              className={`segment${archived === value ? " active" : ""}`}
              aria-pressed={archived === value}
              onClick={() => update({ archived: value === "false" ? "" : value })}
            >
              {label}
            </button>
          ))}
        </div>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={mine}
            onChange={(event) => update({ mine: event.target.checked ? "1" : "" })}
          />
          Only mine
        </label>
      </div>

      {documents.error && !documents.data ? (
        <ErrorState error={documents.error} onRetry={documents.reload} />
      ) : documents.loading && !documents.data ? (
        <div className="card card-body">
          <Skeleton lines={6} />
        </div>
      ) : rows.length === 0 ? (
        <EmptyState
          title={filtered ? "No documents match these filters" : "No documents yet"}
          action={
            filtered ? (
              <Button
                onClick={() => {
                  setSearchInput("");
                  setParams(new URLSearchParams(), { replace: true });
                }}
              >
                Clear filters
              </Button>
            ) : (
              <Button variant="primary" onClick={() => setUploading(true)}>
                Upload a document
              </Button>
            )
          }
        >
          {filtered
            ? "Try a different search or remove a filter."
            : "Upload a regulation, policy, circular or form to make it searchable."}
        </EmptyState>
      ) : (
        <div className="table-wrap" aria-busy={documents.loading}>
          <table className="table">
            <caption className="visually-hidden">Documents</caption>
            <thead>
              <tr>
                <th scope="col">Title</th>
                <th scope="col">Type</th>
                <th scope="col">Department</th>
                <th scope="col">Owner</th>
                <th scope="col">Status</th>
                <th scope="col">Updated</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td>
                    <Link to={`/documents/${row.id}`} className="table-title">
                      {row.title}
                    </Link>
                    <span className="table-sub">
                      {row.current_version
                        ? `${row.current_version.file_name} · v${row.current_version.version_number} · ${formatBytes(row.current_version.file_size)}`
                        : "No file"}
                    </span>
                  </td>
                  <td>{TYPE_LABEL[row.document_type]}</td>
                  <td>{row.department || <span className="muted">—</span>}</td>
                  <td>{row.owner_username || <span className="muted">—</span>}</td>
                  <td>
                    <div className="badge-row">
                      <StatusBadge status={row.status} />
                      {row.is_archived ? <Badge tone="muted">Archived</Badge> : null}
                    </div>
                  </td>
                  <td title={row.updated_at}>{formatRelative(row.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {documents.data ? (
        <Pagination
          page={page}
          count={documents.data.count}
          onPage={(next) => update({ page: String(next) })}
        />
      ) : null}

      <UploadDialog
        open={uploading}
        onClose={() => setUploading(false)}
        onUploaded={(document) => {
          setUploading(false);
          navigate(`/documents/${document.id}`);
        }}
      />
    </div>
  );
}
