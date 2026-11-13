import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";

import type { DashboardActivity, Kpi } from "../api/analytics";
import { api } from "../api/client";
import { homeApi } from "../api/home";
import { usePolling, useResource } from "../api/hooks";
import { notificationsApi } from "../api/notifications";
import type { DashboardDocument, Me } from "../api/types";
import { useMe } from "../auth/AuthContext";
import { BarChart, BarList, Donut, KpiTile, type ValueFormat } from "../components/charts";
import { Deadlines } from "../components/dashboard/Deadlines";
import { Onboarding, useSampleStep, wasDismissed, type OnboardingStep } from "../components/dashboard/Onboarding";
import { FileTypeIcon, Icon } from "../components/icons";
import UploadDialog from "../components/UploadDialog";
import {
  Alert,
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
import { displayName, formatPercent, formatRelative, pluralize } from "../lib/format";
import { TYPE_LABEL, humanize } from "../lib/labels";
import "../styles/dashboard.css";

const today = new Intl.DateTimeFormat(undefined, {
  weekday: "long",
  day: "numeric",
  month: "long",
});

const ratio: ValueFormat = (value) => formatPercent(value, value > 0 && value < 0.1 ? 1 : 0);

function isSystemAdmin(me: Me): boolean {
  return me.is_superuser || me.role === "SYSTEM_ADMIN";
}

/** What this person can see, said the way the server decides it. */
function scopeSentence(me: Me): string {
  if (isSystemAdmin(me)) return "You can see every department's documents.";
  if (me.department) return `You can see documents from ${me.department} and any you uploaded yourself.`;
  return "You can see the documents you uploaded yourself. Ask an administrator to assign you a department.";
}

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

const sum = (values: number[]) => values.reduce((total, value) => total + value, 0);

/** The reader's own numbers, from the dashboard's activity block. */
function personal(activity: DashboardActivity | undefined) {
  const days = activity?.questions_by_day ?? [];
  const counts = days.map((day) => day.count);
  const answered = days.map((day) => day.answered);
  const lastWeek = sum(counts.slice(-7));
  const weekBefore = sum(counts.slice(-14, -7));
  const asked = sum(counts);
  return {
    weeks: activity?.uploads_by_week ?? [],
    days,
    lastWeek,
    weekBefore,
    asked,
    answerRate: asked ? sum(answered) / asked : null,
    dailyRate: days.map((day) => (day.count ? day.answered / day.count : null)),
  };
}

function OrgStrip({ org, loading }: { org: DashboardActivity["org"]; loading: boolean }) {
  const attention = useResource(() => notificationsApi.attention().catch(() => null), []);
  const kpis = org?.kpis;
  const tile = (label: string, kpi: Kpi | undefined, format?: ValueFormat, points = false) => (
    <KpiTile
      label={label}
      loading={loading || !kpi}
      value={kpi?.value ?? null}
      format={format}
      series={kpi?.series}
      href="/admin/analytics"
      delta={
        kpi
          ? { value: kpi.value, previous: kpi.previous, style: points ? "points" : "percent", period: "vs previous week" }
          : undefined
      }
    />
  );
  const counts = attention.data;
  const problems = counts ? counts.failed_documents + counts.stalled_jobs : 0;
  return (
    <section className="home-org" aria-labelledby="home-org-title">
      <div className="home-section-head">
        <h2 id="home-org-title" className="home-section-title">
          Organisation this week
          {org ? (
            <span className="home-section-scope">{org.scope.department ?? "All departments"}</span>
          ) : null}
        </h2>
        <div className="home-org-links">
          {problems ? (
            <Link to="/admin/processing" className="home-attention">
              <Icon name="alert-triangle" size={14} />
              {counts?.failed_documents ? pluralize(counts.failed_documents, "failed document") : null}
              {counts?.failed_documents && counts.stalled_jobs ? " · " : null}
              {counts?.stalled_jobs ? pluralize(counts.stalled_jobs, "stalled run") : null}
            </Link>
          ) : null}
          <Link to="/admin/analytics" className="link link-with-icon">
            Analytics
            <Icon name="arrow-right" size={14} />
          </Link>
        </div>
      </div>
      <div className="home-kpis">
        {tile("Questions asked", kpis?.questions)}
        {tile("Answered from documents", kpis?.answer_rate, ratio, true)}
        {tile("Rated helpful", kpis?.helpful_rate, ratio, true)}
        {tile("Documents processed", kpis?.documents_processed)}
      </div>
    </section>
  );
}

export default function DashboardPage() {
  useDocumentTitle("Dashboard");
  const me = useMe();
  const navigate = useNavigate();
  const dashboard = useResource(() => api.dashboard(), []);
  const suggestions = useResource(() => homeApi.suggestions(), []);
  const deadlines = useResource(() => homeApi.deadlines(90), []);
  const [uploading, setUploading] = useState(false);
  const [question, setQuestion] = useState("");
  const [dismissed, setDismissed] = useState(() => wasDismissed(me.id));

  const data = dashboard.data;
  const counts = data?.documents.by_status ?? {};
  const inFlight = (counts.QUEUED ?? 0) + (counts.PROCESSING ?? 0) + (counts.UPLOADED ?? 0);
  usePolling(dashboard.reload, inFlight > 0, 5000);

  const total = data?.documents.total ?? 0;
  const ready = counts.COMPLETED ?? 0;
  const activity = data?.activity;
  const mine = personal(activity);
  const loading = !data && !dashboard.error;
  const operations = me.capabilities.view_operations;

  // The first-run checklist, ticked off from what has already happened.
  const wantsChecklist = Boolean(data) && !dismissed;
  const collections = useResource(() => homeApi.collectionCount(), [], {
    enabled: wantsChecklist && me.capabilities.create_collections,
  });
  const sampleStep = useSampleStep(
    wantsChecklist && isSystemAdmin(me) && total === 0,
    me.department,
    dashboard.reload,
  );
  const steps: OnboardingStep[] = [
    {
      key: "upload",
      title: "Upload a document",
      detail: "PDFs, Word files, scans and photos are read, indexed and made searchable.",
      icon: "upload",
      done: total > 0,
      action: { label: "Upload", run: () => setUploading(true) },
    },
    {
      key: "ask",
      title: "Ask a question",
      detail: "Answers quote the passages they come from, or say the documents do not cover it.",
      icon: "sparkles",
      done: Boolean(data?.recent_conversations.length) || mine.asked > 0,
      to: "/ask",
    },
    ...(me.capabilities.create_collections
      ? [
          {
            key: "collection",
            title: "Create a collection",
            detail: "Group related documents to search and ask within just them.",
            icon: "folder-plus" as const,
            done: (collections.data ?? 0) > 0,
            to: "/collections",
          },
        ]
      : []),
    ...(data?.pending_signup_requests !== null && data?.pending_signup_requests !== undefined
      ? [
          {
            key: "signups",
            title: "Review sign-up requests",
            detail: "People who asked for an account can sign in once you approve them.",
            icon: "users" as const,
            done: data.pending_signup_requests === 0,
            to: "/admin/users?tab=requests",
          },
        ]
      : []),
    ...(sampleStep ? [sampleStep] : []),
  ];
  const showChecklist = wantsChecklist && steps.some((step) => !step.done);

  const ask = (event: FormEvent) => {
    event.preventDefault();
    const text = question.trim();
    if (text) navigate("/ask", { state: { question: text } });
  };
  const askAbout = (text: string) => navigate("/ask", { state: { question: text } });

  const suggested = (suggestions.data ?? []).slice(0, 4);

  return (
    <div className="page home">
      <PageHeader
        eyebrow={
          <>
            <Icon name="calendar" size={14} />
            {today.format(new Date())}
          </>
        }
        title={`Welcome, ${displayName(me)}`}
        description={scopeSentence(me)}
        actions={
          <Button variant="primary" icon="upload" onClick={() => setUploading(true)}>
            Upload document
          </Button>
        }
      />

      {/* Someone waiting to be let in has no other way to ask. */}
      {data?.pending_signup_requests ? (
        <Alert
          tone="info"
          title={`${pluralize(data.pending_signup_requests, "sign-up request")} waiting for you`}
          action={
            <Link to="/admin/users?tab=requests" className="btn btn-secondary btn-sm">
              Review
            </Link>
          }
        >
          {data.pending_signup_requests === 1 ? "Someone has" : "People have"} asked for an
          account and can sign in once you approve.
        </Alert>
      ) : null}

      {showChecklist ? <Onboarding userId={me.id} steps={steps} onDismiss={() => setDismissed(true)} /> : null}

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
        {suggested.length ? (
          <div className="suggestion-row" aria-label="Suggested questions" role="group">
            {suggested.map((suggestion) => (
              <button
                key={`${suggestion.document_id}-${suggestion.question}`}
                type="button"
                className="chip"
                title={`From “${suggestion.document_title}”`}
                onClick={() => askAbout(suggestion.question)}
              >
                <Icon name="sparkles" />
                {suggestion.question}
              </button>
            ))}
          </div>
        ) : suggestions.loading || !data ? null : total === 0 ? (
          <p className="home-ask-empty">
            <Icon name="upload-cloud" size={16} />
            Questions are answered from your library.{" "}
            <button type="button" className="link-button" onClick={() => setUploading(true)}>
              Upload a document
            </button>{" "}
            to start asking.
          </p>
        ) : (
          <div className="suggestion-row" aria-label="Example questions" role="group">
            {EXAMPLE_QUESTIONS.map((example) => (
              <button key={example} type="button" className="chip" onClick={() => askAbout(example)}>
                <Icon name="sparkles" />
                {example}
              </button>
            ))}
          </div>
        )}
      </form>

      {dashboard.error && !data ? <ErrorState error={dashboard.error} onRetry={dashboard.reload} /> : null}

      <section className="home-kpis" aria-label="Your library and questions">
        <KpiTile
          label="Documents"
          icon="files"
          loading={loading}
          value={data ? total : null}
          series={mine.weeks.length ? mine.weeks.map((week) => week.count) : undefined}
          caption={
            mine.weeks.length
              ? `${pluralize(mine.weeks[mine.weeks.length - 1].count, "upload")} this week`
              : "In your library"
          }
        />
        <KpiTile
          label="Ready to search"
          icon="check-circle"
          loading={loading}
          value={data ? ready : null}
          color={3}
          caption={
            inFlight
              ? `${pluralize(inFlight, "document")} still processing`
              : total
                ? `${formatPercent(ready / total)} of your library`
                : "Searchable once processed"
          }
        />
        <KpiTile
          // Not "Your questions…": Ask's composer is the one control labelled
          // "Your question" (e2e locates it by label, which is a substring
          // match, while this page may still be on screen).
          label="Questions you asked this week"
          icon="message"
          loading={loading}
          value={data ? mine.lastWeek : null}
          series={mine.days.length ? mine.days.map((day) => day.count) : undefined}
          delta={activity ? { value: mine.lastWeek, previous: mine.weekBefore, period: "vs the week before" } : undefined}
          caption={activity ? `${pluralize(mine.asked, "question")} in 30 days` : undefined}
        />
        <KpiTile
          label="Answered from documents"
          icon="quote"
          loading={loading}
          value={mine.answerRate}
          format={ratio}
          color={2}
          series={mine.asked ? mine.dailyRate : undefined}
          caption={mine.asked ? "Of your questions in the last 30 days" : "Ask a question to see this"}
        />
      </section>

      {operations && (loading || activity?.org) ? <OrgStrip org={activity?.org ?? null} loading={loading} /> : null}

      <div className="dash-grid">
        <div className="stack-lg">
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

          {activity ? (
            <Card title="Your activity" icon="activity" description="Uploads to the documents you can see, and the questions you asked.">
              <div className="home-charts">
                <BarChart
                  integer
                  height={180}
                  title="Uploads per week, last 12 weeks"
                  labels={mine.weeks.map((week) => week.week)}
                  series={[{ key: "uploads", label: "Files uploaded", values: mine.weeks.map((week) => week.count) }]}
                  empty="Nothing uploaded in the last 12 weeks."
                />
                <BarChart
                  stacked
                  integer
                  height={180}
                  title="Questions you asked per day, last 30 days"
                  labels={mine.days.map((day) => day.date)}
                  series={[
                    { key: "answered", label: "Answered from documents", values: mine.days.map((day) => day.answered) },
                    { key: "other", label: "Not from documents", values: mine.days.map((day) => day.count - day.answered) },
                  ]}
                  empty="You have not asked anything in the last 30 days."
                />
              </div>
            </Card>
          ) : null}
        </div>

        <div className="stack-lg">
          {deadlines.data?.length ? <Deadlines deadlines={deadlines.data} /> : null}

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

          {activity && activity.documents_by_type.length ? (
            <Card title="Your library" icon="layers" description="What the documents you can see are, and where they belong.">
              <div className="stack">
                <Donut
                  title="Documents by type"
                  size={132}
                  centerLabel="documents"
                  items={activity.documents_by_type.map((row) => ({
                    key: row.document_type,
                    label: TYPE_LABEL[row.document_type] ?? humanize(row.document_type),
                    value: row.count,
                  }))}
                />
                {activity.documents_by_department.length > 1 ? (
                  <BarList
                    title="Documents by department"
                    items={activity.documents_by_department.map((row) => ({
                      key: row.department || "—",
                      label: row.department || "No department",
                      value: row.count,
                    }))}
                  />
                ) : null}
              </div>
            </Card>
          ) : null}
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
