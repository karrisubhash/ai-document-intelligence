import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useSearchParams } from "react-router-dom";

import { api, type DocumentQuery } from "../api/client";
import { useDebouncedValue, usePolling, useResource } from "../api/hooks";
import { DOCUMENT_STATUSES, DOCUMENT_TYPES } from "../api/types";
import { FileTypeIcon, Icon } from "../components/icons";
import UploadDialog from "../components/UploadDialog";
import {
  Avatar,
  Badge,
  Button,
  Dialog,
  EmptyState,
  ErrorState,
  PageHeader,
  Pagination,
  SelectBox,
  Skeleton,
  StatusBadge,
  plural,
  useDocumentTitle,
  useToast,
} from "../components/ui";
import { formatBytes, formatRelative, pluralize } from "../lib/format";
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
  const location = useLocation();
  const [params, setParams] = useSearchParams();
  // Quick find's "Upload a document" lands here with the dialog open.
  const [uploading, setUploading] = useState(
    Boolean((location.state as { upload?: boolean } | null)?.upload),
  );
  const clearedHandoff = useRef(false);
  useEffect(() => {
    if (clearedHandoff.current || !(location.state as { upload?: boolean } | null)?.upload) return;
    clearedHandoff.current = true;
    // Cleared at once, or a reload would open the dialog again.
    navigate(`${location.pathname}${location.search}`, { replace: true, state: {} });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const page = Number(params.get("page") || "1") || 1;
  const status = params.get("status") ?? "";
  const documentType = params.get("type") ?? "";
  const department = params.get("department") ?? "";
  const ordering = params.get("sort") ?? "-updated_at";
  const archived = (params.get("archived") as "false" | "true" | "all" | null) ?? "false";
  const mine = params.get("mine") === "1";

  const [searchInput, setSearchInput] = useState(params.get("q") ?? "");
  const searchField = useRef<HTMLInputElement>(null);
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
  // The button that cleared them disappears with the filters; focus goes to
  // the start of the filters rather than being lost to the page.
  const clearFilters = () => {
    setSearchInput("");
    setParams(new URLSearchParams(), { replace: true });
    searchField.current?.focus();
  };
  const total = documents.data?.count ?? rows.length;

  // Selection belongs to one listing: a different page, filter or sort
  // starts empty, so nothing off-screen is ever deleted by surprise.
  // `allMatching` widens it to every matching document, on every page.
  const [selected, setSelected] = useState<Set<number>>(() => new Set());
  const [allMatching, setAllMatching] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const selectAllBox = useRef<HTMLInputElement>(null);
  const listing = JSON.stringify([page, search, status, documentType, department, ordering, archived, mine]);
  useEffect(() => {
    setSelected(new Set());
    setAllMatching(false);
  }, [listing]);

  const selectable = rows.filter((row) => row.can_manage);
  // A reload (polling, another tab's change) can drop a selected row from
  // this page; it is dropped from the selection too, so what is deleted is
  // always what is ticked on screen.
  useEffect(() => {
    const onScreen = new Set(selectable.map((row) => row.id));
    setSelected((current) =>
      [...current].every((id) => onScreen.has(id))
        ? current
        : new Set([...current].filter((id) => onScreen.has(id))),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [documents.data]);

  const pageSelected = selectable.length > 0 && selectable.every((row) => selected.has(row.id));
  const someSelected = selectable.some((row) => selected.has(row.id));
  const showSelection = selectable.length > 0 || selected.size > 0;

  const toggle = (id: number, on: boolean) => {
    // Unticking one while "all matching" is on keeps the rest of the page.
    const next = new Set(allMatching ? selectable.map((row) => row.id) : selected);
    if (on) next.add(id);
    else next.delete(id);
    setAllMatching(false);
    setSelected(next);
  };
  const togglePage = (on: boolean) => {
    setAllMatching(false);
    setSelected((current) => {
      const next = new Set(current);
      for (const row of selectable) {
        if (on) next.add(row.id);
        else next.delete(row.id);
      }
      return next;
    });
  };
  // The buttons that cleared the selection go with it; focus stays put.
  const clearSelection = () => {
    setSelected(new Set());
    setAllMatching(false);
    selectAllBox.current?.focus();
  };
  const afterDelete = (answered: number[], completed: boolean) => {
    const gone = new Set(answered);
    setSelected((current) => new Set([...current].filter((id) => !gone.has(id))));
    if (completed) setAllMatching(false);
    // After the dialog has closed: while it is modal, the page is inert.
    window.setTimeout(() => selectAllBox.current?.focus(), 0);
    // Leave the page only if deleting emptied it.
    const emptied = rows.every((row) => gone.has(row.id));
    if (page > 1 && (emptied || (completed && allMatching))) update({ page: "" });
    else documents.reload();
  };

  const filters: Omit<DocumentQuery, "page" | "ordering"> = {
    search,
    status,
    document_type: documentType,
    department,
    archived,
    owner: mine ? "me" : undefined,
  };


  return (
    <div className="page">
      <PageHeader
        title="Documents"
        description="Everything you have access to. Uploads are scanned, read and indexed in the background."
        actions={
          <Button variant="primary" icon="upload" onClick={() => setUploading(true)}>
            Upload document
          </Button>
        }
      />

      <div className="filters" role="search" aria-label="Filter documents">
        <div className="toolbar">
          <div className="input-icon toolbar-search">
            <Icon name="search" />
            <input
              ref={searchField}
              type="search"
              className="input"
              placeholder="Search titles and file names…"
              aria-label="Search titles and file names"
              value={searchInput}
              onChange={(event) => setSearchInput(event.target.value)}
            />
          </div>
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
        </div>
        <div className="toolbar">
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
          <span className="toolbar-spacer" />
          <div className="list-summary" aria-live="polite">
            {rows.length ? (
              <>
                <span>
                  {pluralize(total, "document")}
                  {filtered ? (total === 1 ? " matches these filters" : " match these filters") : ""}
                </span>
                {filtered ? (
                  <button type="button" className="link-button" onClick={clearFilters}>
                    Clear filters
                  </button>
                ) : null}
              </>
            ) : null}
          </div>
        </div>
      </div>

      {documents.error && !documents.data ? (
        <ErrorState error={documents.error} onRetry={documents.reload} />
      ) : documents.loading && !documents.data ? (
        <div className="card card-body">
          <Skeleton lines={6} />
        </div>
      ) : rows.length === 0 ? (
        <div className="card">
          <EmptyState
            title={filtered ? "No documents match these filters" : "No documents yet"}
            icon={filtered ? "filter" : "upload-cloud"}
            action={
              filtered ? (
                <Button icon="x" onClick={clearFilters}>
                  Clear filters
                </Button>
              ) : (
                <Button variant="primary" icon="upload" onClick={() => setUploading(true)}>
                  Upload a document
                </Button>
              )
            }
          >
            {filtered
              ? "Try a different search or remove a filter."
              : "Upload a regulation, policy, circular or form to make it searchable."}
          </EmptyState>
        </div>
      ) : (
        <div className="stack">
          {/* Mounted whenever rows can be selected - appearing on the first
              tick, it pushed the table down by a row under the pointer. */}
          {showSelection ? (
            <div
              className={selected.size > 0 || allMatching ? "bulk-bar" : "bulk-bar bulk-bar-idle"}
              role="region"
              aria-label="Selected documents"
            >
              <span className="bulk-bar-count" aria-live="polite">
                {allMatching
                  ? `All matching documents selected (${total})`
                  : selected.size
                    ? `${selected.size} selected`
                    : "Select documents to delete them"}
              </span>
              {/* Only when there is more than this page to select. */}
              {selected.size > 0 && !allMatching && total > rows.length ? (
                <Button variant="ghost" size="sm" onClick={() => setAllMatching(true)}>
                  Select all {total} matching
                </Button>
              ) : null}
              {selected.size > 0 || allMatching ? (
                <>
                  <Button variant="ghost" size="sm" icon="x" onClick={clearSelection}>
                    Clear selection
                  </Button>
                  <Button
                    variant="danger"
                    size="sm"
                    icon="trash"
                    className="bulk-bar-action"
                    onClick={() => setConfirmingDelete(true)}
                  >
                    {allMatching ? "Delete all…" : "Delete selected"}
                  </Button>
                </>
              ) : null}
            </div>
          ) : null}
          <div className="table-wrap" aria-busy={documents.loading}>
            <table className="table">
              <caption className="visually-hidden">Documents</caption>
              <thead>
                <tr>
                  {showSelection ? (
                    <th scope="col" className="table-select">
                      <SelectBox
                        inputRef={selectAllBox}
                        label="Select all on this page"
                        checked={allMatching || pageSelected}
                        indeterminate={!allMatching && someSelected && !pageSelected}
                        disabled={selectable.length === 0}
                        onChange={togglePage}
                      />
                    </th>
                  ) : null}
                  <th scope="col">Title</th>
                  <th scope="col">Type</th>
                  <th scope="col" className="col-secondary">
                    Department
                  </th>
                  <th scope="col" className="col-secondary">
                    Owner
                  </th>
                  <th scope="col">Status</th>
                  <th scope="col" className="col-secondary">
                    Updated
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr
                    key={row.id}
                    className={selected.has(row.id) || (allMatching && row.can_manage) ? "selected" : undefined}
                  >
                    {showSelection ? (
                      <td className="table-select">
                        <SelectBox
                          label={`Select ${row.title}`}
                          checked={row.can_manage && (allMatching || selected.has(row.id))}
                          disabled={!row.can_manage}
                          title={row.can_manage ? undefined : "You may not delete this document"}
                          onChange={(on) => toggle(row.id, on)}
                        />
                      </td>
                    ) : null}
                    <td>
                      <div className="cell-title">
                        <FileTypeIcon fileName={row.current_version?.file_name} />
                        <div className="cell-title-text">
                          <Link to={`/documents/${row.id}`} className="table-title">
                            {row.title}
                          </Link>
                          <span className="table-sub">
                            {row.current_version
                              ? `${row.current_version.file_name} · v${row.current_version.version_number} · ${formatBytes(row.current_version.file_size)}`
                              : "No file"}
                          </span>
                        </div>
                      </div>
                    </td>
                    <td>{TYPE_LABEL[row.document_type]}</td>
                    <td className="col-secondary">
                      {row.department || <span className="muted">—</span>}
                    </td>
                    <td className="col-secondary">
                      {row.owner_username ? (
                        <span className="cell-person">
                          <Avatar name={row.owner_username} size="sm" />
                          {row.owner_username}
                        </span>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                    <td>
                      <div className="badge-row">
                        <StatusBadge status={row.status} />
                        {row.is_archived ? <Badge tone="muted">Archived</Badge> : null}
                      </div>
                    </td>
                    <td className="nowrap muted col-secondary" title={row.updated_at}>
                      {formatRelative(row.updated_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
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

      <BulkDeleteDialog
        open={confirmingDelete}
        target={allMatching ? { all: true, filters } : { ids: [...selected] }}
        onClose={() => setConfirmingDelete(false)}
        onFinished={(answered, completed) => {
          if (completed) setConfirmingDelete(false);
          afterDelete(answered, completed);
        }}
      />
    </div>
  );
}

type DeleteTarget =
  | { ids: number[] }
  | { all: true; filters: Omit<DocumentQuery, "page" | "ordering"> };

/** What the server takes per request; larger selections are sent in batches. */
const BATCH = 100;
const CONFIRM_WORD = "DELETE";

interface Preview {
  /** Matching documents the user may delete, as of `asOf`. */
  count: number;
  /** Matching documents the user may see but not delete. */
  skipped: number;
  /** The server's snapshot: documents created later are never included. */
  asOf: string;
}

function BulkDeleteDialog({
  open,
  target,
  onClose,
  onFinished,
}: {
  open: boolean;
  target: DeleteTarget;
  onClose: () => void;
  /**
   * The ids the server answered for (deleted, or already gone), and whether
   * the run completed. Called after a failure too - what was deleted before
   * it is gone, and the listing must say so.
   */
  onFinished: (answered: number[], completed: boolean) => void;
}) {
  const toast = useToast();
  const [typed, setTyped] = useState("");
  const [pending, setPending] = useState(false);
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState<unknown>(null);
  // What the user confirms is fixed when the dialog opens; polling and other
  // tabs keep changing the listing underneath it.
  const [job, setJob] = useState<DeleteTarget>(target);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [previewError, setPreviewError] = useState<unknown>(null);
  // Leaving the page stops the run between requests, and nothing touches
  // the page (or the URL) after that.
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    if (!open) return;
    setTyped("");
    setProgress(0);
    setError(null);
    setPreview(null);
    setPreviewError(null);
    setJob(target);
    if (!("all" in target)) return;
    // "All matching" is counted by the server, not taken from the listing:
    // the listing counts documents the user may not delete, and the count
    // comes with the snapshot the delete then keeps to.
    let current = true;
    api
      .bulkDeleteDocuments({ all: true, filters: target.filters, preview: true })
      .then((result) => {
        if (current)
          setPreview({
            count: result.remaining ?? 0,
            skipped: result.skipped ?? 0,
            asOf: result.as_of ?? "",
          });
      })
      .catch((caught: unknown) => {
        if (current) setPreviewError(caught);
      });
    return () => {
      current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const all = "all" in job;
  const count = all ? (preview?.count ?? 0) : job.ids.length;
  const noun = plural(count, "document");
  const ready = all ? preview !== null && preview.count > 0 : count > 0;
  const armed = ready && (!all || typed.trim().toUpperCase() === CONFIRM_WORD);

  const run = async () => {
    setPending(true);
    setError(null);
    const answered: number[] = [];
    const failed = new Set<number>();
    let deleted = 0;
    let refused = 0;
    let completed = false;
    try {
      if ("all" in job) {
        // Rounds of at most BATCH, all within the preview's snapshot. Stop
        // when nothing remains, or when a round deletes nothing - documents
        // that keep failing must not spin this forever.
        for (;;) {
          const result = await api.bulkDeleteDocuments({
            all: true,
            filters: job.filters,
            as_of: preview?.asOf,
          });
          answered.push(...result.deleted, ...result.not_found);
          for (const id of result.failed ?? []) failed.add(id);
          deleted += result.deleted.length;
          refused = result.skipped ?? 0;
          if (alive.current) setProgress(deleted);
          if (!alive.current || !result.remaining || result.deleted.length === 0) break;
        }
      } else {
        for (let start = 0; start < job.ids.length && alive.current; start += BATCH) {
          const result = await api.bulkDeleteDocuments({
            ids: job.ids.slice(start, start + BATCH),
          });
          const forbidden = result.forbidden ?? [];
          answered.push(...result.deleted, ...result.not_found, ...forbidden);
          for (const id of result.failed ?? []) failed.add(id);
          deleted += result.deleted.length;
          refused += forbidden.length;
          if (alive.current) setProgress(deleted);
        }
      }
      completed = true;
    } catch (caught) {
      if (alive.current) setError(caught);
    }
    if (!alive.current) return;
    setPending(false);

    const parts = [`Deleted ${deleted} ${plural(deleted, "document")}.`];
    if (!completed) parts[0] = `Deleted ${deleted} ${plural(deleted, "document")} before deleting stopped.`;
    if (refused) parts.push(`${refused} you may not delete ${refused === 1 ? "was" : "were"} left in place.`);
    if (failed.size) parts.push(`${failed.size} could not be deleted - try again.`);
    if (completed && deleted === 0 && refused > 0 && !failed.size) {
      toast("Nothing was deleted: you may not delete these documents.", "warning");
    } else if (completed || deleted > 0) {
      toast(parts.join(" "), completed && !refused && !failed.size ? "success" : "warning");
    }
    onFinished(answered, completed);
  };

  return (
    <Dialog
      open={open}
      onClose={() => {
        if (!pending) onClose();
      }}
      title={
        all
          ? preview
            ? `Delete ${count} matching ${noun}?`
            : "Delete all matching documents?"
          : `Delete ${count} ${noun}?`
      }
      size="sm"
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={pending}>
            Cancel
          </Button>
          <Button variant="danger" onClick={run} loading={pending} disabled={!armed}>
            {all ? "Delete all permanently" : "Delete permanently"}
          </Button>
        </>
      }
    >
      <div className="stack">
        <p>
          {count === 1 ? "The document is" : "Each document is"} removed permanently, with every
          version, its extracted text, its search index and its files. This cannot be undone.
        </p>
        {all ? (
          <p>
            That is every document matching the current search and filters that you may delete, on
            every page - not only the ones shown. Documents added from now on are not included.
          </p>
        ) : null}
        {all && !preview && !previewError ? (
          <p className="muted" role="status">
            Counting the matching documents…
          </p>
        ) : null}
        {preview && preview.skipped > 0 ? (
          <p className="muted">
            {preview.skipped} matching {plural(preview.skipped, "document")} you may not delete{" "}
            {preview.skipped === 1 ? "is" : "are"} left in place.
          </p>
        ) : null}
        {preview && preview.count === 0 ? (
          <p role="status">None of the matching documents are yours to delete.</p>
        ) : null}
        {previewError ? (
          <ErrorState error={previewError} title="Could not count the matching documents" />
        ) : null}
        <p className="muted">To take documents out of search without losing them, archive them instead.</p>
        {all && preview && preview.count > 0 ? (
          <label className="field">
            <span className="field-label">Type {CONFIRM_WORD} to confirm</span>
            <input
              className="input"
              value={typed}
              autoComplete="off"
              spellCheck={false}
              disabled={pending}
              onChange={(event) => setTyped(event.target.value)}
            />
          </label>
        ) : null}
        {pending && progress > 0 ? (
          <p role="status">
            Deleted {progress} of {count}…
          </p>
        ) : null}
        {error ? (
          <>
            <ErrorState error={error} title="Deleting stopped" />
            {progress > 0 ? (
              <p>
                {progress} {plural(progress, "document")} {progress === 1 ? "was" : "were"} deleted
                before it stopped; the list has been refreshed.
              </p>
            ) : null}
          </>
        ) : null}
      </div>
    </Dialog>
  );
}
