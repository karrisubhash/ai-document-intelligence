import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";

import { api } from "../api/client";
import { usePolling, useResource } from "../api/hooks";
import type { DashboardDocument } from "../api/types";
import { useMe } from "../auth/AuthContext";
import { FileTypeIcon, Icon, type IconName } from "../components/icons";
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
import { EXAMPLE_QUESTIONS } from "../lib/examples";
import { displayName, formatRelative, pluralize } from "../lib/format";
import { TYPE_LABEL } from "../lib/labels";

const today = new Intl.DateTimeFormat(undefined, {
  weekday: "long",
  day: "numeric",
  month: "long",
});

function DocumentRows({ rows }: { rows: DashboardDocument[] }) {
  return (
    <ul className="row-list row-list-flush">
      {rows.map((row) => (
        <li key={row.id} className="row-item">
          <Link to={`/documents/${row.id}`} className="row-link">
            <FileTypeIcon fileName={row.file_name} />
            <span className="row-text">
              <span className="row-title">{row.title}</span>
              <span className="row-meta">
                {TYPE_LABEL[row.document_type]}
                {row.department ? ` · ${row.department}` : ""} · {formatRelative(row.updated_at)}
              </span>
            </span>
          </Link>
          <StatusBadge status={row.status} />
        </li>
      ))}
    </ul>
  );
}

function Stat({
  label,
  value,
  caption,
  icon,
  tone,
}: {
  label: string;
  value: number | undefined;
  caption: string;
  icon: IconName;
  tone: "neutral" | "success" | "info" | "danger";
}) {
  return (
    <div className="stat">
      <div className="stat-head">
        <span className="stat-label">{label}</span>
        <span className={`stat-icon stat-icon-${tone}`} aria-hidden="true">
          <Icon name={icon} />
        </span>
      </div>
      <span className="stat-value">{value === undefined ? "—" : value.toLocaleString()}</span>
      <span className="stat-caption">{caption}</span>
    </div>
  );
}

export default function DashboardPage() {
  useDocumentTitle("Dashboard");
  const me = useMe();
  const navigate = useNavigate();
  const dashboard = useResource(() => api.dashboard(), []);
  const [uploading, setUploading] = useState(false);
  const [question, setQuestion] = useState("");

  const data = dashboard.data;
  const counts = data?.documents.by_status ?? {};
  const inFlight = (counts.QUEUED ?? 0) + (counts.PROCESSING ?? 0) + (counts.UPLOADED ?? 0);
  usePolling(dashboard.reload, inFlight > 0, 5000);

  const total = data?.documents.total ?? 0;
  const ready = counts.COMPLETED ?? 0;
  const failed = (counts.FAILED ?? 0) + (counts.QUARANTINED ?? 0);
  // Until the counts arrive, a caption must not claim anything about them.
  const pending = dashboard.error ? "Unavailable" : "Loading…";

  const ask = (event: FormEvent) => {
    event.preventDefault();
    const text = question.trim();
    if (text) navigate("/ask", { state: { question: text } });
  };

  return (
    <div className="page">
      <PageHeader
        eyebrow={
          <>
            <Icon name="calendar" size={14} />
            {today.format(new Date())}
          </>
        }
        title={`Welcome, ${displayName(me)}`}
        description={
          me.department
            ? `You can see documents from ${me.department} and any you uploaded yourself.`
            : "You can see the documents you uploaded yourself. Ask an administrator to assign you a department."
        }
        actions={
          <Button variant="primary" icon="upload" onClick={() => setUploading(true)}>
            Upload document
          </Button>
        }
      />

      <form className="ask-hero" onSubmit={ask} role="search" aria-label="Ask a question">
        <label htmlFor="dashboard-question" className="ask-hero-label">
          <span className="hero-mark" aria-hidden="true">
            <Icon name="sparkles" size={18} />
          </span>
          Ask your documents a question
        </label>
        <div className="ask-box">
          <input
            id="dashboard-question"
            className="ask-box-input"
            placeholder="Ask a question about your documents…"
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            maxLength={2000}
          />
          <Button type="submit" variant="primary" iconEnd="arrow-right" disabled={!question.trim()}>
            Ask
          </Button>
        </div>
        <div className="ask-hero-footer">
          <p className="ask-hero-hint">
            <Icon name="shield" size={14} />
            Answers cite the passages they come from. When the documents do not say, the
            assistant says so instead of guessing.
          </p>
        </div>
        <div className="suggestion-row">
          {EXAMPLE_QUESTIONS.map((example) => (
            <button
              key={example}
              type="button"
              className="chip"
              onClick={() => navigate("/ask", { state: { question: example } })}
            >
              <Icon name="sparkles" />
              {example}
            </button>
          ))}
        </div>
      </form>

      {dashboard.error && !data ? (
        <ErrorState error={dashboard.error} onRetry={dashboard.reload} />
      ) : null}

      <section className="stats" aria-label="Document summary">
        <Stat
          label="Documents"
          value={data ? total : undefined}
          caption={data ? "In your library" : pending}
          icon="files"
          tone="neutral"
        />
        <Stat
          label="Ready"
          value={data ? ready : undefined}
          caption={
            !data
              ? pending
              : total
                ? `${Math.round((ready / total) * 100)}% searchable`
                : "Searchable once processed"
          }
          icon="check-circle"
          tone="success"
        />
        <Stat
          label="Processing"
          value={data ? inFlight : undefined}
          caption={!data ? pending : inFlight ? "Updates automatically" : "Nothing in the queue"}
          icon="activity"
          tone="info"
        />
        <Stat
          label="Failed"
          value={data ? failed : undefined}
          caption={!data ? pending : failed ? "Open one to see why" : "Nothing needs attention"}
          icon="alert-triangle"
          tone={failed ? "danger" : "neutral"}
        />
      </section>

      <div className="dash-grid">
        <Card
          title="Recently updated"
          icon="history"
          padded={false}
          actions={
            <Link to="/documents" className="link link-with-icon">
              All documents
              <Icon name="arrow-right" size={14} />
            </Link>
          }
        >
          {dashboard.loading && !data ? (
            <div className="card-body">
              <Skeleton lines={4} />
            </div>
          ) : !data ? (
            <p className="card-empty">Recent documents could not be loaded.</p>
          ) : data.recent_documents.length ? (
            <DocumentRows rows={data.recent_documents} />
          ) : (
            <EmptyState
              title="No documents yet"
              icon="upload-cloud"
              action={
                <Button variant="primary" icon="upload" onClick={() => setUploading(true)}>
                  Upload the first one
                </Button>
              }
            >
              Uploaded documents are scanned, read, indexed and made searchable.
            </EmptyState>
          )}
        </Card>

        <div className="stack-lg">
          {data?.needs_attention.length ? (
            <Card
              title="Needs attention"
              icon="alert-triangle"
              description="Open one to see which stage failed and why."
              padded={false}
            >
              <DocumentRows rows={data.needs_attention} />
            </Card>
          ) : null}

          <Card
            title="Recent conversations"
            icon="message"
            padded={false}
            actions={
              <Link to="/ask" className="link link-with-icon">
                New question
                <Icon name="plus" size={14} />
              </Link>
            }
          >
            {dashboard.loading && !data ? (
              <div className="card-body">
                <Skeleton lines={3} />
              </div>
            ) : !data ? (
              <p className="card-empty">Recent conversations could not be loaded.</p>
            ) : data.recent_conversations.length ? (
              <ul className="row-list row-list-flush">
                {data.recent_conversations.map((conversation) => (
                  <li key={conversation.id} className="row-item">
                    <Link to={`/ask/${conversation.id}`} className="row-link">
                      <span className="icon-tile" aria-hidden="true">
                        <Icon name="message" />
                      </span>
                      <span className="row-text">
                        <span className="row-title">
                          {conversation.title || "Untitled conversation"}
                        </span>
                        <span className="row-meta">
                          {pluralize(Math.ceil(conversation.message_count / 2), "question")} ·{" "}
                          {formatRelative(conversation.updated_at)}
                        </span>
                      </span>
                    </Link>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="card-empty">Your questions and their answers are kept here.</p>
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
        onBatchUploaded={() => {
          setUploading(false);
          navigate("/documents");
        }}
      />
    </div>
  );
}
