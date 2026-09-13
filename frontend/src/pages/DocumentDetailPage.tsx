import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { api } from "../api/client";
import { usePolling, useResource } from "../api/hooks";
import { DOCUMENT_TYPES, type DocumentType } from "../api/types";
import { useToken } from "../auth/AuthContext";
import ChunksTab from "../components/tabs/ChunksTab";
import ClassificationTab from "../components/tabs/ClassificationTab";
import ContentTab from "../components/tabs/ContentTab";
import FieldsTab from "../components/tabs/FieldsTab";
import PipelineTab from "../components/tabs/PipelineTab";
import {
  Empty,
  ErrorBox,
  Field,
  Pill,
  Spinner,
  Tabs,
  formatBytes,
  formatTime,
  toneForDocumentStatus,
} from "../components/ui";

const TABS = [
  { id: "pipeline", label: "Pipeline" },
  { id: "content", label: "Content" },
  { id: "classification", label: "Classification" },
  { id: "fields", label: "Fields" },
  { id: "chunks", label: "Chunks" },
  { id: "versions", label: "Versions" },
] as const;

type TabId = (typeof TABS)[number]["id"];

const IN_FLIGHT = new Set(["UPLOADED", "QUEUED", "PROCESSING"]);

export default function DocumentDetailPage() {
  const token = useToken();
  const navigate = useNavigate();
  const params = useParams();
  const documentId = Number(params.id);

  const [tab, setTab] = useState<TabId>("pipeline");
  const [action, setAction] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const document = useResource(() => api.document(token, documentId), [token, documentId]);
  const jobs = useResource(() => api.processing(token, documentId), [token, documentId]);
  const versions = useResource(() => api.versions(token, documentId), [token, documentId]);

  const inFlight =
    (document.data ? IN_FLIGHT.has(document.data.status) : false) ||
    (jobs.data ?? []).some((job) => job.status === "QUEUED" || job.status === "PROCESSING");

  usePolling(() => {
    document.reload();
    jobs.reload();
  }, inFlight);

  // Derived output (content, classification, fields, chunks) only changes
  // when the pipeline writes something, so keying the tabs on the
  // document's own updated_at refreshes them exactly then - and not on
  // every poll tick.
  const refreshKey = document.data
    ? `${document.data.status}:${document.data.updated_at}`
    : "initial";

  async function run(label: string, task: () => Promise<unknown>) {
    setBusy(true);
    setAction(null);
    setNotice(null);

    try {
      await task();
      setNotice(label);
      document.reload();
      jobs.reload();
      versions.reload();
    } catch (caught) {
      setAction(caught);
    } finally {
      setBusy(false);
    }
  }

  if (document.loading && !document.data) return <Spinner />;
  if (document.error) return <ErrorBox error={document.error} />;
  if (!document.data) return null;

  const doc = document.data;
  const version = doc.current_version;

  return (
    <div className="stack">
      <div className="card">
        <div className="card-head">
          <div>
            <Link to="/documents" className="link">
              ← Documents
            </Link>
            <h2 className="title-row">
              {doc.title}
              <Pill tone={toneForDocumentStatus(doc.status)}>{doc.status}</Pill>
              {inFlight ? <Pill tone="busy">following…</Pill> : null}
            </h2>
          </div>
        </div>

        <div className="fields">
          <Field label="ID">{doc.id}</Field>
          <Field label="Type">
            <select
              value={doc.document_type}
              disabled={busy}
              onChange={(event) =>
                run("Type updated.", () =>
                  api.updateDocument(token, doc.id, {
                    document_type: event.target.value as DocumentType,
                  }),
                )
              }
            >
              {DOCUMENT_TYPES.map((type) => (
                <option key={type} value={type}>
                  {type}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Department">
            {doc.department || <span className="muted">unset</span>}
          </Field>
          <Field label="Owner">{doc.owner ?? "—"}</Field>
          <Field label="Uploaded">{formatTime(doc.created_at)}</Field>
          <Field label="Updated">{formatTime(doc.updated_at)}</Field>
        </div>

        {version ? (
          <div className="fields">
            <Field label="Version">v{version.version_number}</Field>
            <Field label="File">{version.file_name}</Field>
            <Field label="Size">{formatBytes(version.file_size)}</Field>
            <Field label="MIME">{version.mime_type || "—"}</Field>
            <Field label="SHA-256">
              <code className="mono small">{version.checksum}</code>
            </Field>
          </div>
        ) : (
          <Empty>This document has no version.</Empty>
        )}

        <div className="actions">
          <button
            className="button"
            disabled={busy}
            onClick={() =>
              run("202 Accepted — reprocess queued.", () => api.reprocess(token, doc.id))
            }
          >
            Reprocess
          </button>

          <button
            className="button button-quiet"
            disabled={busy || !version}
            onClick={() =>
              run("Downloaded.", () =>
                api.download(token, doc.id, version?.file_name ?? `document-${doc.id}`),
              )
            }
          >
            Download original
          </button>

          <label className="button button-quiet file-button">
            Upload new version
            <input
              type="file"
              disabled={busy}
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (!file) return;
                void run("202 Accepted — new version queued.", () =>
                  api.uploadVersion(token, doc.id, file),
                );
                event.target.value = "";
              }}
            />
          </label>

          <button
            className="button button-danger"
            disabled={busy}
            onClick={() => {
              if (!window.confirm(`Delete "${doc.title}" and all of its versions?`)) {
                return;
              }
              void api
                .deleteDocument(token, doc.id)
                .then(() => navigate("/documents"))
                .catch(setAction);
            }}
          >
            Delete
          </button>
        </div>

        {notice ? <div className="banner banner-ok">{notice}</div> : null}
        <ErrorBox error={action} />
      </div>

      <div className="card">
        <Tabs tabs={TABS} active={tab} onChange={setTab} />

        <div className="tab-body">
          {tab === "pipeline" ? (
            <>
              <ErrorBox error={jobs.error} />
              <PipelineTab jobs={jobs.data ?? []} />
            </>
          ) : null}

          {tab === "content" ? (
            <ContentTab documentId={doc.id} refreshKey={refreshKey} />
          ) : null}

          {tab === "classification" ? (
            <ClassificationTab documentId={doc.id} refreshKey={refreshKey} />
          ) : null}

          {tab === "fields" ? (
            <FieldsTab documentId={doc.id} refreshKey={refreshKey} />
          ) : null}

          {tab === "chunks" ? (
            <ChunksTab documentId={doc.id} refreshKey={refreshKey} />
          ) : null}

          {tab === "versions" ? (
            <>
              <ErrorBox error={versions.error} />
              {(versions.data ?? []).length === 0 ? (
                <Empty>No versions recorded.</Empty>
              ) : (
                <table className="table">
                  <thead>
                    <tr>
                      <th>#</th>
                      <th>File</th>
                      <th>Size</th>
                      <th>MIME</th>
                      <th>SHA-256</th>
                      <th>Uploaded</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(versions.data ?? []).map((item) => (
                      <tr key={item.id}>
                        <td>
                          v{item.version_number}
                          {version?.id === item.id ? (
                            <span className="muted small"> · current</span>
                          ) : null}
                        </td>
                        <td>{item.file_name}</td>
                        <td className="muted small">{formatBytes(item.file_size)}</td>
                        <td className="muted small">{item.mime_type || "—"}</td>
                        <td className="mono small truncate">{item.checksum}</td>
                        <td className="muted small">{formatTime(item.created_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
}
