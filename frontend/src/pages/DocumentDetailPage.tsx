import { useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { ApiError, api, saveBlob, type DocumentPatch } from "../api/client";
import { useAction, usePolling, useResource } from "../api/hooks";
import { DOCUMENT_TYPES, type DocumentDetail, type DocumentType } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import {
  ContentPanel,
  FieldsPanel,
  FiguresPanel,
  PreviewPanel,
  ProcessingPanel,
} from "../components/document/Panels";
import { usePageTrail } from "../components/Layout";
import VersionsPanel from "../components/document/VersionsPanel";
import { FileTypeIcon, Icon } from "../components/icons";
import { InsightsOverview, typeProvenance } from "../components/document/Insights";
import {
  Alert,
  Badge,
  Button,
  ConfirmDialog,
  Dialog,
  ErrorState,
  Field,
  Menu,
  MenuItem,
  MenuSeparator,
  PageHeader,
  Skeleton,
  StatusBadge,
  TabPanel,
  Tabs,
  errorMessage,
  useDocumentTitle,
  useToast,
  type TabItem,
} from "../components/ui";
import { formatBytes } from "../lib/format";
import { IN_FLIGHT, TYPE_LABEL } from "../lib/labels";

const TABS: TabItem[] = [
  { id: "overview", label: "Overview", icon: "info" },
  { id: "preview", label: "Preview", icon: "eye" },
  { id: "text", label: "Text", icon: "book-open" },
  { id: "fields", label: "Fields", icon: "list-checks" },
  { id: "figures", label: "Figures", icon: "image" },
  { id: "versions", label: "Versions", icon: "layers" },
  { id: "processing", label: "Processing", icon: "activity" },
];

/** Which count belongs to which tab. There is no Tables tab: a table is text
 * (one labelled line per row, "Table 2, row 1: ..."), read in the Text tab. */
const COUNTED: Record<string, "fields_found" | "figures"> = {
  fields: "fields_found",
  figures: "figures",
};

/**
 * The tabs this document has: Fields and Figures carry their count,
 * and are left out when it is zero - an empty tab is a click that finds
 * nothing. Kept while selected, so a deep link to one still opens it.
 * Before the counts are known (an older API), every tab is shown.
 */
export function tabsFor(document: DocumentDetail, selected: string): TabItem[] {
  const counts = document.counts;
  if (!counts) return TABS;
  return TABS.flatMap((item) => {
    const key = COUNTED[item.id];
    if (!key) return [item];
    const count = counts[key];
    if (count === 0 && item.id !== selected) return [];
    return [{ ...item, count }];
  });
}

function EditDialog({
  document,
  open,
  onClose,
  onSaved,
  onStale,
}: {
  document: DocumentDetail;
  open: boolean;
  onClose: () => void;
  onSaved: (document: DocumentDetail) => void;
  /** The document changed under the form: load it again. */
  onStale: () => void;
}) {
  const [title, setTitle] = useState(document.title);
  // The type the person picked, and the one the list showed when they did -
  // null until they touch it. Until then the list shows the type the
  // document has now, following the page as it reloads, and a save sends no
  // type at all. The type detector sets the type while a document is
  // processing; a form that sent back the type it opened with turned that
  // stale value into "chosen by a person", which the detector never
  // overrides again.
  const [picked, setPicked] = useState<{ type: DocumentType; from: DocumentType } | null>(
    null,
  );
  const type = picked?.type ?? document.document_type;
  const save = useAction((next: DocumentPatch) => api.updateDocument(document.id, next));
  const fieldErrors = save.error instanceof ApiError ? save.error.fieldErrors() : {};

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const result = await save.run({
      title: title.trim(),
      // A change only, and with the type it was made from: the server
      // refuses it if the type has moved since.
      ...(picked && picked.type !== picked.from
        ? { document_type: picked.type, expected_document_type: picked.from }
        : {}),
    });
    if (result.ok) {
      onSaved(result.value);
    } else if (result.error instanceof ApiError && result.error.status === 409) {
      // Refused: the type changed while the form was open. The list goes
      // back to showing the document's type - reloaded now - so a second
      // choice is made knowing what it replaces. The message says why.
      setPicked(null);
      onStale();
    }
  };

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Edit details"
      size="sm"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" type="submit" form="edit-document" loading={save.pending}>
            Save
          </Button>
        </>
      }
    >
      <form id="edit-document" className="stack" onSubmit={submit}>
        <Field label="Title" error={fieldErrors.title} required>
          {(props) => (
            <input
              {...props}
              className="input"
              value={title}
              maxLength={255}
              onChange={(event) => setTitle(event.target.value)}
            />
          )}
        </Field>
        <Field label="Type" error={fieldErrors.document_type}>
          {(props) => (
            <select
              {...props}
              className="input"
              value={type}
              onChange={(event) =>
                setPicked({
                  type: event.target.value as DocumentType,
                  from: picked?.from ?? document.document_type,
                })
              }
            >
              {DOCUMENT_TYPES.map((value) => (
                <option key={value} value={value}>
                  {TYPE_LABEL[value]}
                </option>
              ))}
            </select>
          )}
        </Field>
        <p className="muted small">
          The department cannot be changed here: it decides who may read the document.
        </p>
        {save.error && !Object.keys(fieldErrors).length ? (
          <Alert tone="danger">{errorMessage(save.error)}</Alert>
        ) : null}
      </form>
    </Dialog>
  );
}

export default function DocumentDetailPage() {
  const { id } = useParams();
  const documentId = Number(id);
  const navigate = useNavigate();
  const toast = useToast();
  const [params, setParams] = useSearchParams();
  const tab = TABS.some((item) => item.id === params.get("tab")) ? params.get("tab")! : "overview";

  const resource = useResource(() => api.document(documentId), [documentId], {
    enabled: Number.isFinite(documentId),
  });
  const document = resource.data;
  useDocumentTitle(document?.title ?? "Document");
  // While it processes, the page watches the PROCESSING TIMELINE, which is
  // not an audited read, and reloads the document only when the run's status
  // moves. Polling the document itself wrote a "viewed the document" audit
  // row every three seconds, burying real views under status checks.
  const lastRun = useRef<string | null>(null);
  usePolling(
    () => {
      api.processing(documentId).then(
        (jobs) => {
          const latest = jobs.length ? `${jobs[0].id}:${jobs[0].status}` : "";
          // A run that has SETTLED always reloads the document, whatever was
          // seen before: a fast document can finish before the first poll,
          // and treating that first sighting as the baseline left the page
          // on "Processing" for good (caught by the browser suite).
          const settled = jobs.length > 0 && !["QUEUED", "PROCESSING"].includes(jobs[0].status);
          if (settled || (lastRun.current !== null && latest !== lastRun.current)) {
            resource.reload();
          }
          lastRun.current = latest;
        },
        () => undefined, // a missed tick; the next one tries again
      );
    },
    Boolean(document && IN_FLIGHT.includes(document.status)),
  );

  const [editing, setEditing] = useState(false);
  const [confirm, setConfirm] = useState<"delete" | "archive" | null>(null);

  const reprocess = useAction(() => api.reprocess(documentId));
  const archive = useAction(() =>
    document?.is_archived ? api.restore(documentId) : api.archive(documentId),
  );
  const remove = useAction(() => api.deleteDocument(documentId));
  const [downloading, setDownloading] = useState(false);
  const { me } = useAuth();
  // The API refuses it too; the button says why instead of failing on click.
  const quarantineBlocksDownload =
    document?.status === "QUARANTINED" && me?.role !== "SYSTEM_ADMIN" && !me?.is_superuser;

  const selectTab = (next: string) => {
    const nextParams = new URLSearchParams();
    nextParams.set("tab", next);
    setParams(nextParams, { replace: true });
  };

  usePageTrail(document ? [{ label: "Documents", to: "/documents" }, { label: document.title }] : null);

  if (!Number.isFinite(documentId)) return <ErrorState error={new Error("Invalid document id")} />;
  if (resource.loading && !document) {
    return (
      <div className="page">
        <Skeleton lines={3} />
      </div>
    );
  }
  // Only when there is nothing to show. A failed REFRESH keeps the page
  // (and any open dialog or upload) and says so below.
  if (!document) {
    return (
      <div className="page">
        <ErrorState
          error={resource.error}
          onRetry={resource.reload}
          title="This document could not be opened"
        />
        <p>
          <Link to="/documents" className="link link-with-icon">
            <Icon name="chevron-left" size={14} />
            Back to documents
          </Link>
        </p>
      </div>
    );
  }

  const version = document.current_version;
  const askable = document.status === "COMPLETED" && !document.is_archived;
  const page = Number(params.get("page") || "1") || 1;
  const start = params.has("start") ? Number(params.get("start")) : null;
  const end = params.has("end") ? Number(params.get("end")) : null;

  const runReprocess = async () => {
    const result = await reprocess.run();
    if (result.ok) {
      toast("Reprocessing started.");
      resource.reload();
      selectTab("processing");
    }
  };

  const download = async () => {
    if (!version) return;
    setDownloading(true);
    try {
      saveBlob(await api.fileBlob(document.id), version.file_name);
    } catch (caught) {
      toast(errorMessage(caught), "danger");
    } finally {
      setDownloading(false);
    }
  };

  return (
    <div className="page">
      {resource.error ? (
        <Alert tone="warning" title="This document could not be refreshed">
          What is shown may be out of date.{" "}
          <button type="button" className="link-button" onClick={resource.reload}>
            Try again
          </button>
        </Alert>
      ) : null}

      <PageHeader
        icon={<FileTypeIcon fileName={version?.file_name} size="lg" />}
        title={document.title}
        description={
          <span className="doc-meta">
            <StatusBadge status={document.status} />
            <Badge
              tone="neutral"
              icon={document.document_type_source === "auto" ? "sparkles" : undefined}
              title={typeProvenance(document) ?? undefined}
            >
              {TYPE_LABEL[document.document_type]}
            </Badge>
            {document.department ? (
              <Badge tone="neutral" icon="building">
                {document.department}
              </Badge>
            ) : null}
            {document.is_archived ? (
              <Badge tone="muted" icon="archive">
                Archived
              </Badge>
            ) : null}
            {version ? (
              <span className="doc-meta-file">
                {version.file_name} · v{version.version_number} · {formatBytes(version.file_size)}
              </span>
            ) : null}
          </span>
        }
        actions={
          <>
            <Button
              // Primary only when it can be used: a disabled primary was the
              // loudest thing on a failed document's page.
              variant={askable ? "primary" : "secondary"}
              icon="sparkles"
              className="page-action-main"
              disabled={!askable}
              title={
                document.is_archived
                  ? "Archived documents are not searched"
                  : document.status === "FAILED"
                    ? "Processing failed, so there is no text to ask about yet"
                    : document.status === "QUARANTINED"
                      ? "Quarantined documents are not searched"
                      : document.status !== "COMPLETED"
                        ? "Available once processing has finished"
                        : undefined
              }
              onClick={() =>
                navigate("/ask", { state: { documentId: document.id, documentTitle: document.title } })
              }
            >
              Ask about this document
            </Button>
            <Button
              icon="download"
              onClick={download}
              loading={downloading}
              disabled={!version || quarantineBlocksDownload}
              title={
                quarantineBlocksDownload
                  ? "Quarantined by the security scan - an administrator can review it"
                  : undefined
              }
            >
              {/* Icon only on a phone; the word stays as the button's name. */}
              <span className="btn-label-wide">Download</span>
            </Button>
            {document.can_manage ? (
              <Menu
                trigger={
                  reprocess.pending ? (
                    <span className="spinner spinner-inline" aria-hidden="true" />
                  ) : (
                    <Icon name="more" />
                  )
                }
                triggerClassName="btn btn-secondary btn-icon"
                triggerLabel="More actions"
                busy={reprocess.pending}
              >
                <MenuItem icon="pencil" onSelect={() => setEditing(true)}>
                  Edit details
                </MenuItem>
                <MenuItem
                  icon="refresh"
                  onSelect={runReprocess}
                  disabled={document.is_archived || !version || reprocess.pending}
                  title={document.is_archived ? "Restore the document first" : undefined}
                >
                  Reprocess
                </MenuItem>
                <MenuItem
                  icon={document.is_archived ? "restore" : "archive"}
                  onSelect={() => setConfirm("archive")}
                >
                  {document.is_archived ? "Restore" : "Archive"}
                </MenuItem>
                <MenuSeparator />
                <MenuItem icon="trash" tone="danger" onSelect={() => setConfirm("delete")}>
                  Delete permanently
                </MenuItem>
              </Menu>
            ) : null}
          </>
        }
      />

      {reprocess.error ? (
        <Alert
          tone={reprocess.error instanceof ApiError && reprocess.error.status === 409 ? "info" : "danger"}
          title={
            reprocess.error instanceof ApiError && reprocess.error.status === 409
              ? "Already processing"
              : "Could not reprocess"
          }
        >
          {reprocess.error instanceof ApiError && reprocess.error.status === 409
            ? "A run for this version is already queued or in progress. Follow it on the Processing tab."
            : errorMessage(reprocess.error)}
        </Alert>
      ) : null}

      {document.status === "QUARANTINED" ? (
        <Alert tone="warning" title="Quarantined by the security scan">
          This file was not processed because the scan found something it will not open —
          for example a disguised executable, macros or active content. It is kept for review
          but is not searchable or previewable.
        </Alert>
      ) : document.status === "FAILED" ? (
        <Alert
          tone="danger"
          title="Processing failed"
          action={
            <div className="button-row">
              <Button size="sm" icon="activity" onClick={() => selectTab("processing")}>
                See what failed
              </Button>
              {document.can_manage ? (
                <Button
                  size="sm"
                  // The next step - primary only when it can be taken.
                  variant={document.is_archived || !version ? "secondary" : "primary"}
                  icon="refresh"
                  onClick={runReprocess}
                  loading={reprocess.pending}
                  disabled={document.is_archived || !version}
                  title={
                    document.is_archived
                      ? "Restore the document first"
                      : !version
                        ? "There is no file to process"
                        : undefined
                  }
                >
                  Reprocess
                </Button>
              ) : null}
            </div>
          }
        >
          The Processing tab shows which stage failed and why.
          {document.can_manage ? " You can reprocess it after fixing the cause." : ""}
        </Alert>
      ) : document.is_archived ? (
        <Alert tone="info" title="Archived">
          Archived documents keep their history but are left out of search and answers.
        </Alert>
      ) : null}

      <div>
        <Tabs
          tabs={tabsFor(document, tab)}
          selected={tab}
          onSelect={selectTab}
          label="Document sections"
        />
        <TabPanel id={tab}>
          {tab === "overview" ? (
            <InsightsOverview document={document} onEditType={() => setEditing(true)} />
          ) : null}
          {tab === "preview" ? <PreviewPanel document={document} /> : null}
          {tab === "text" ? (
            <ContentPanel
              document={document}
              page={page}
              start={start}
              end={end}
              onPage={(next) => {
                const nextParams = new URLSearchParams();
                nextParams.set("tab", "text");
                nextParams.set("page", String(next));
                setParams(nextParams, { replace: true });
              }}
            />
          ) : null}
          {tab === "fields" ? <FieldsPanel document={document} /> : null}
          {tab === "figures" ? <FiguresPanel document={document} /> : null}
          {tab === "versions" ? (
            <VersionsPanel document={document} onChanged={resource.reload} />
          ) : null}
          {tab === "processing" ? (
            <ProcessingPanel document={document} onSettled={resource.reload} />
          ) : null}
        </TabPanel>
      </div>

      {editing ? (
        <EditDialog
          document={document}
          open={editing}
          onClose={() => setEditing(false)}
          onSaved={(saved) => {
            setEditing(false);
            resource.setData({ ...document, ...saved });
            toast("Details saved.");
          }}
          onStale={resource.reload}
        />
      ) : null}

      <ConfirmDialog
        open={confirm === "archive"}
        title={document.is_archived ? "Restore this document?" : "Archive this document?"}
        confirmLabel={document.is_archived ? "Restore" : "Archive"}
        tone="primary"
        pending={archive.pending}
        error={archive.error}
        onClose={() => {
          archive.clearError();
          setConfirm(null);
        }}
        onConfirm={async () => {
          const result = await archive.run();
          if (result.ok) {
            resource.setData({ ...document, ...result.value });
            setConfirm(null);
            toast(result.value.is_archived ? "Document archived." : "Document restored.");
          }
        }}
      >
        <p>
          {document.is_archived
            ? "It will be searchable again and included in answers straight away — nothing needs re-indexing."
            : "It stays readable here with its full history, but is left out of search and answers and accepts no new versions until restored."}
        </p>
      </ConfirmDialog>

      <ConfirmDialog
        open={confirm === "delete"}
        title="Delete this document permanently?"
        confirmLabel="Delete permanently"
        pending={remove.pending}
        error={remove.error}
        onClose={() => {
          remove.clearError();
          setConfirm(null);
        }}
        onConfirm={async () => {
          const result = await remove.run();
          if (result.ok) {
            toast("Document deleted.");
            navigate("/documents", { replace: true });
          }
        }}
      >
        <p>
          Every version, its extracted text and its search index are removed, and the files
          are deleted from storage. The audit trail keeps a record that it existed. This
          cannot be undone — archive it instead if it may be needed again.
        </p>
      </ConfirmDialog>
    </div>
  );
}
