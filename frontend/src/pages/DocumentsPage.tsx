import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import { useResource, usePolling } from "../api/hooks";
import { DOCUMENT_TYPES, type DocumentType } from "../api/types";
import { useAuth, useToken } from "../auth/AuthContext";
import {
  Empty,
  ErrorBox,
  Pill,
  Spinner,
  formatBytes,
  formatTime,
  toneForDocumentStatus,
} from "../components/ui";

const IN_FLIGHT = new Set(["UPLOADED", "QUEUED", "PROCESSING"]);

function UploadForm({ onUploaded }: { onUploaded: () => void }) {
  const token = useToken();
  const { me } = useAuth();

  const [title, setTitle] = useState("");
  const [documentType, setDocumentType] = useState<DocumentType>("GENERAL");
  const [department, setDepartment] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [accepted, setAccepted] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Only a system admin may file a document outside their own department;
  // the API pins everyone else's upload to their profile regardless of
  // what is sent, so offering the input to them would be a lie.
  const canChooseDepartment = me?.role === "SYSTEM_ADMIN";

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!file) return;

    setBusy(true);
    setError(null);
    setAccepted(null);

    try {
      const created = await api.upload(token, {
        title,
        document_type: documentType,
        department: canChooseDepartment ? department : undefined,
        file,
      });

      setAccepted(
        `202 Accepted — document #${created.id} queued. Processing runs in the worker.`,
      );
      setTitle("");
      setFile(null);
      (event.target as HTMLFormElement).reset();
      onUploaded();
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card" onSubmit={submit}>
      <h2>Upload</h2>

      <div className="row">
        <label className="grow">
          <span>Title</span>
          <input
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            placeholder="Exam Regulations 2024"
            required
          />
        </label>

        <label>
          <span>Type</span>
          <select
            value={documentType}
            onChange={(event) => setDocumentType(event.target.value as DocumentType)}
          >
            {DOCUMENT_TYPES.map((type) => (
              <option key={type} value={type}>
                {type}
              </option>
            ))}
          </select>
        </label>

        {canChooseDepartment ? (
          <label>
            <span>Department</span>
            <input
              value={department}
              onChange={(event) => setDepartment(event.target.value)}
              placeholder={me?.department || "unset"}
            />
          </label>
        ) : null}
      </div>

      <label>
        <span>File — PDF, DOCX, TXT or image</span>
        <input
          type="file"
          accept=".pdf,.docx,.txt,.png,.jpg,.jpeg,.tif,.tiff"
          onChange={(event) => setFile(event.target.files?.[0] ?? null)}
          required
        />
      </label>

      <ErrorBox error={error} />
      {accepted ? <div className="banner banner-ok">{accepted}</div> : null}

      <button className="button" type="submit" disabled={busy || !file}>
        {busy ? "Uploading…" : "Upload"}
      </button>
    </form>
  );
}

export default function DocumentsPage() {
  const token = useToken();
  const [page, setPage] = useState(1);

  const documents = useResource(() => api.documents(token, page), [token, page]);

  // Follow anything still moving through the pipeline. Upload returns 202
  // and the worker does the rest, so without this the list would show a
  // queued document forever until someone reloaded the page.
  const inFlight = (documents.data?.results ?? []).some((doc) =>
    IN_FLIGHT.has(doc.status),
  );
  usePolling(documents.reload, inFlight);

  return (
    <div className="stack">
      <UploadForm onUploaded={documents.reload} />

      <section className="card">
        <div className="card-head">
          <h2>Documents</h2>
          <div className="card-head-right">
            {inFlight ? <Pill tone="busy">following the queue…</Pill> : null}
            <button className="button button-quiet" onClick={documents.reload}>
              Refresh
            </button>
          </div>
        </div>

        <ErrorBox error={documents.error} />

        {documents.loading && !documents.data ? <Spinner /> : null}

        {documents.data && documents.data.results.length === 0 ? (
          <Empty>
            No documents visible to you. That is either an empty instance or the
            department boundary doing its job — the list shows exactly what you may
            retrieve, nothing more.
          </Empty>
        ) : null}

        {documents.data && documents.data.results.length > 0 ? (
          <table className="table">
            <thead>
              <tr>
                <th>#</th>
                <th>Title</th>
                <th>Type</th>
                <th>Department</th>
                <th>Status</th>
                <th>Current version</th>
                <th>Uploaded</th>
              </tr>
            </thead>
            <tbody>
              {documents.data.results.map((doc) => (
                <tr key={doc.id}>
                  <td className="muted">{doc.id}</td>
                  <td>
                    <Link to={`/documents/${doc.id}`}>{doc.title}</Link>
                  </td>
                  <td>{doc.document_type}</td>
                  <td>{doc.department || <span className="muted">unset</span>}</td>
                  <td>
                    <Pill tone={toneForDocumentStatus(doc.status)}>{doc.status}</Pill>
                  </td>
                  <td className="muted small">
                    {doc.current_version
                      ? `v${doc.current_version.version_number} · ${doc.current_version.file_name} · ${formatBytes(doc.current_version.file_size)}`
                      : "none"}
                  </td>
                  <td className="muted small">{formatTime(doc.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}

        {documents.data && (documents.data.next || documents.data.previous) ? (
          <div className="pager">
            <button
              className="button button-quiet"
              disabled={!documents.data.previous}
              onClick={() => setPage((current) => current - 1)}
            >
              Previous
            </button>
            <span className="muted small">
              page {page} · {documents.data.count} document(s)
            </span>
            <button
              className="button button-quiet"
              disabled={!documents.data.next}
              onClick={() => setPage((current) => current + 1)}
            >
              Next
            </button>
          </div>
        ) : null}
      </section>
    </div>
  );
}
