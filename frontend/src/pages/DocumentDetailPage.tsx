import { useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { ApiError, api, saveBlob } from "../api/client";
import { useAction, usePolling, useResource } from "../api/hooks";
import { DOCUMENT_TYPES, type DocumentDetail, type DocumentType } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import {
  ContentPanel,
  FieldsPanel,
  FiguresPanel,
  PreviewPanel,
  ProcessingPanel,
  TablesPanel,
} from "../components/document/Panels";
import { usePageTrail } from "../components/Layout";
import VersionsPanel from "../components/document/VersionsPanel";
import { FileTypeIcon, Icon } from "../components/icons";
import {
  Alert,
  Badge,
  Button,
  Card,
  ConfirmDialog,
  DescriptionList,
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
import { formatLabel } from "../lib/files";
import { formatBytes, formatDateTime, formatPercent } from "../lib/format";
import { IN_FLIGHT, TYPE_LABEL, humanize } from "../lib/labels";

const TABS: TabItem[] = [
  { id: "overview", label: "Overview", icon: "info" },
  { id: "preview", label: "Preview", icon: "eye" },
  { id: "text", label: "Text", icon: "book-open" },
  { id: "fields", label: "Fields", icon: "list-checks" },
  { id: "tables", label: "Tables", icon: "table" },
  { id: "figures", label: "Figures", icon: "image" },
  { id: "versions", label: "Versions", icon: "layers" },
  { id: "processing", label: "Processing", icon: "activity" },
];

function Overview({ document }: { document: DocumentDetail }) {
  const classification = useResource(
    () => api.classification(document.id),
    [document.id, document.status],
  );
  const version = document.current_version;
  const verdict = classification.data?.[0];

  return (
    <div className="grid-2">
      <Card title="Details" icon="info">
        <DescriptionList
          items={[
            ["Type", TYPE_LABEL[document.document_type]],
            ["Department", document.department || "None (visible to the owner only)"],
            ["Owner", document.owner_username || "—"],
            ["Status", <StatusBadge key="s" status={document.status} />],
            ["Created", formatDateTime(document.created_at)],
            ["Last updated", formatDateTime(document.updated_at)],
            ...(document.is_archived
              ? ([["Archived", formatDateTime(document.archived_at)]] as [string, string][])
              : []),
          ]}
        />
      </Card>
      <div className="stack-lg">
        <Card title="Current file" icon="file">
          {version ? (
            <DescriptionList
              items={[
                ["File", version.file_name],
                ["Version", `v${version.version_number}`],
                ["Size", formatBytes(version.file_size)],
                [
                  "Format",
                  <span key="f" title={version.mime_type || undefined}>
                    {formatLabel(version.mime_type)}
                  </span>,
                ],
                ["Uploaded", formatDateTime(version.created_at)],
              ]}
            />
          ) : (
            <p className="muted">No file has been uploaded.</p>
          )}
        </Card>
        <Card title="Classification" icon="cpu">
          {classification.loading && !classification.data ? (
            <Skeleton lines={2} />
          ) : classification.error && !classification.data ? (
            // A failed REQUEST is not a document that was never classified.
            <ErrorState
              error={classification.error}
              onRetry={classification.reload}
              title="The classification could not be loaded"
            />
          ) : verdict ? (
            <DescriptionList
              items={[
                [
                  "Predicted type",
                  verdict.predicted_label ? humanize(verdict.predicted_label) : "Unsure — abstained",
                ],
                ["Confidence", formatPercent(verdict.confidence)],
                ["Model", `${verdict.model_name} ${verdict.model_version}`],
              ]}
            />
          ) : (
            <p className="muted">
              Not classified. Classification runs only when a classifier is configured; it
              never guesses a label.
            </p>
          )}
        </Card>
      </div>
    </div>
  );
}

function EditDialog({
  document,
  open,
  onClose,
  onSaved,
}: {
  document: DocumentDetail;
  open: boolean;
  onClose: () => void;
  onSaved: (document: DocumentDetail) => void;
}) {
  const [title, setTitle] = useState(document.title);
  const [type, setType] = useState<DocumentType>(document.document_type);
  const save = useAction((next: { title: string; document_type: DocumentType }) =>
    api.updateDocument(document.id, next),
  );
  const fieldErrors = save.error instanceof ApiError ? save.error.fieldErrors() : {};

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const result = await save.run({ title: title.trim(), document_type: type });
    if (result.ok) onSaved(result.value);
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
              onChange={(event) => setType(event.target.value as DocumentType)}
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
            <Badge tone="neutral">{TYPE_LABEL[document.document_type]}</Badge>
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
                  variant="primary"
                  icon="refresh"
                  onClick={runReprocess}
                  loading={reprocess.pending}
                  disabled={document.is_archived || !version}
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
        <Tabs tabs={TABS} selected={tab} onSelect={selectTab} label="Document sections" />
        <TabPanel id={tab}>
          {tab === "overview" ? <Overview document={document} /> : null}
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
          {tab === "tables" ? <TablesPanel document={document} /> : null}
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
