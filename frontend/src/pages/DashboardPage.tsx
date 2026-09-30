import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";

import { api } from "../api/client";
import { usePolling, useResource } from "../api/hooks";
import type { DashboardDocument } from "../api/types";
import { useMe } from "../auth/AuthContext";
import UploadDialog from "../components/UploadDialog";
import {
  Button,
  Card,
  EmptyState,
  ErrorState,
  PageHeader,
  Skeleton,
  StatusBadge,
  useDocumentTitle,
} from "../components/ui";
import { displayName, formatRelative } from "../lib/format";
import { TYPE_LABEL } from "../lib/labels";

function DocumentRows({ rows }: { rows: DashboardDocument[] }) {
  return (
    <ul className="row-list">
      {rows.map((row) => (
        <li key={row.id} className="row-item">
          <Link to={`/documents/${row.id}`} className="row-link">
            <span className="row-title">{row.title}</span>
            <span className="row-meta">
              {TYPE_LABEL[row.document_type]}
              {row.department ? ` · ${row.department}` : ""} · {formatRelative(row.updated_at)}
            </span>
          </Link>
          <StatusBadge status={row.status} />
        </li>
      ))}
    </ul>
  );
}

export default function DashboardPage() {
  useDocumentTitle("Dashboard");
  const me = useMe();
  const navigate = useNavigate();
  const dashboard = useResource(() => api.dashboard(), []);
  const [uploading, setUploading] = useState(false);
  const [question, setQuestion] = useState("");

  const counts = dashboard.data?.documents.by_status ?? {};
  const inFlight = (counts.QUEUED ?? 0) + (counts.PROCESSING ?? 0) + (counts.UPLOADED ?? 0);
  usePolling(dashboard.reload, inFlight > 0, 5000);

  const ask = (event: FormEvent) => {
    event.preventDefault();
    const text = question.trim();
    if (text) navigate("/ask", { state: { question: text } });
  };

  return (
    <div className="page">
      <PageHeader
        title={`Welcome, ${displayName(me)}`}
        description={
          me.department
            ? `You can see documents from ${me.department} and any you uploaded yourself.`
            : "You can see the documents you uploaded yourself. Ask an administrator to assign you a department."
        }
        actions={
          <Button variant="primary" onClick={() => setUploading(true)}>
            Upload document
          </Button>
        }
      />

      <form className="ask-hero" onSubmit={ask} role="search" aria-label="Ask a question">
        <label htmlFor="dashboard-question" className="ask-hero-label">
          Ask your documents a question
        </label>
        <div className="ask-hero-row">
          <input
            id="dashboard-question"
            className="input input-lg"
            placeholder="e.g. What is the minimum attendance required to sit the end-semester exam?"
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            maxLength={2000}
          />
          <Button type="submit" variant="primary" disabled={!question.trim()}>
            Ask
          </Button>
        </div>
        <p className="ask-hero-hint">
          Answers cite the passages they come from. When the documents do not say, the
          assistant says so instead of guessing.
        </p>
      </form>

      {dashboard.error && !dashboard.data ? (
        <ErrorState error={dashboard.error} onRetry={dashboard.reload} />
      ) : null}

      <div className="stats" aria-label="Document summary">
        {[
          ["Documents", dashboard.data?.documents.total],
          ["Ready", counts.COMPLETED],
          ["Processing", inFlight],
          ["Failed", (counts.FAILED ?? 0) + (counts.QUARANTINED ?? 0)],
        ].map(([label, value]) => (
          <div className="stat" key={label as string}>
            <span className="stat-value">
              {dashboard.data ? ((value as number | undefined) ?? 0).toLocaleString() : "—"}
            </span>
            <span className="stat-label">{label}</span>
          </div>
        ))}
      </div>

      <div className="grid-2">
        <Card
          title="Recently updated"
          actions={
            <Link to="/documents" className="link">
              All documents
            </Link>
          }
        >
          {dashboard.loading && !dashboard.data ? (
            <Skeleton lines={4} />
          ) : dashboard.data?.recent_documents.length ? (
            <DocumentRows rows={dashboard.data.recent_documents} />
          ) : (
            <EmptyState
              title="No documents yet"
              action={
                <Button variant="primary" onClick={() => setUploading(true)}>
                  Upload the first one
                </Button>
              }
            >
              Uploaded documents are scanned, read, indexed and made searchable.
            </EmptyState>
          )}
        </Card>

        <div className="stack">
          {dashboard.data?.needs_attention.length ? (
            <Card title="Needs attention">
              <p className="muted small">
                These could not be processed. Open one to see which stage failed and why.
              </p>
              <DocumentRows rows={dashboard.data.needs_attention} />
            </Card>
          ) : null}

          <Card
            title="Recent conversations"
            actions={
              <Link to="/ask" className="link">
                New question
              </Link>
            }
          >
            {dashboard.loading && !dashboard.data ? (
              <Skeleton lines={3} />
            ) : dashboard.data?.recent_conversations.length ? (
              <ul className="row-list">
                {dashboard.data.recent_conversations.map((conversation) => (
                  <li key={conversation.id} className="row-item">
                    <Link to={`/ask/${conversation.id}`} className="row-link">
                      <span className="row-title">
                        {conversation.title || "Untitled conversation"}
                      </span>
                      <span className="row-meta">
                        {Math.ceil(conversation.message_count / 2)} question
                        {conversation.message_count > 2 ? "s" : ""} ·{" "}
                        {formatRelative(conversation.updated_at)}
                      </span>
                    </Link>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="muted">Your questions and their answers are kept here.</p>
            )}
          </Card>
        </div>
      </div>

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
