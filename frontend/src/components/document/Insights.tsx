import type { ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";

import { api } from "../../api/client";
import { usePolling, useResource } from "../../api/hooks";
import {
  insightsApi,
  spanLink,
  type DocumentInsights,
  type EntityType,
  type InsightEntity,
  type InsightStats,
  type KeyPointKind,
  type RelatedDocuments,
} from "../../api/insights";
import type { ClassificationResult, DocumentDetail } from "../../api/types";
import { formatBytes, formatDateTime, formatPercent } from "../../lib/format";
import { IN_FLIGHT, TYPE_LABEL, humanize } from "../../lib/labels";
import { Icon, type IconName } from "../icons";
import { Badge, Card, DescriptionList, ErrorState, Skeleton } from "../ui";
import "../../styles/insights.css";

/*
 * The Overview, insights first: what the document says (its brief, key
 * points and questions) before what it is (type, file, owner). Everything
 * shown here was extracted from the document itself, and every sentence and
 * fact links to the exact place it was read from.
 */

const LANGUAGE_LABEL: Record<InsightStats["language"], string | null> = {
  en: "English",
  hi: "Hindi",
  te: "Telugu",
  mixed: "Mixed languages",
  unknown: null,
};

const KIND_LABEL: Record<KeyPointKind, string> = {
  deadline: "Deadline",
  obligation: "Requirement",
  figure: "Figure",
  date: "Date",
  clause: "Clause",
  heading: "Section",
};

const KIND_TONE: Record<KeyPointKind, "warning" | "info" | "neutral" | "success"> = {
  deadline: "warning",
  obligation: "info",
  figure: "success",
  date: "neutral",
  clause: "neutral",
  heading: "neutral",
};

interface FactGroup {
  title: string;
  icon: IconName;
  types: EntityType[];
}

const FACT_GROUPS: FactGroup[] = [
  { title: "Dates and deadlines", icon: "calendar", types: ["DEADLINE", "DATE"] },
  { title: "Amounts and figures", icon: "gauge", types: ["MONEY", "PERCENT", "DURATION"] },
  { title: "References and codes", icon: "hash", types: ["REFERENCE", "COURSE_CODE"] },
  { title: "Contacts and links", icon: "mail", types: ["EMAIL", "PHONE", "URL"] },
  { title: "Organisations", icon: "building", types: ["ORG"] },
];

/** "12 pages · 3,400 words · 15 min read · 4 pages read by OCR · English" */
export function healthLine(stats: InsightStats): string {
  const parts: string[] = [];
  if (stats.pages) parts.push(`${stats.pages} ${stats.pages === 1 ? "page" : "pages"}`);
  if (stats.words) parts.push(`${stats.words.toLocaleString("en-IN")} words`);
  if (stats.reading_minutes) parts.push(`${stats.reading_minutes} min read`);
  if (stats.ocr_pages) {
    parts.push(
      stats.ocr_pages === stats.pages
        ? "scanned, read by OCR"
        : `${stats.ocr_pages} ${stats.ocr_pages === 1 ? "page" : "pages"} read by OCR`,
    );
  }
  const language = LANGUAGE_LABEL[stats.language];
  if (language) parts.push(language);
  return parts.join(" · ");
}

/** How the type was decided, in words: "Auto-detected · 82%". */
export function typeProvenance(document: DocumentDetail): string | null {
  if (document.document_type_source === "auto") {
    return document.type_confidence != null
      ? `Auto-detected · ${formatPercent(document.type_confidence)}`
      : "Auto-detected";
  }
  if (document.document_type_source === "user") return "Chosen by a person";
  if (document.document_type_source === "default" && document.document_type === "GENERAL") {
    return "No specific type detected";
  }
  return null;
}

function formatDate(iso: string | null): string | null {
  if (!iso || !/^\d{4}-\d{2}-\d{2}$/.test(iso)) return null;
  const [year, month, day] = iso.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day)).toLocaleDateString("en-IN", {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
}

function BriefState({ insights, document }: { insights: DocumentInsights; document: DocumentDetail }) {
  if (insights.status === "PENDING") {
    return (
      <div className="brief-pending" role="status">
        <span className="spinner spinner-inline" aria-hidden="true" />
        <div>
          <p className="brief-pending-title">Reading this document…</p>
          <p className="muted small">
            Its summary, key facts and suggested questions appear here in a moment.
          </p>
        </div>
      </div>
    );
  }
  if (insights.status === "FAILED") {
    return (
      <p className="muted">
        The brief could not be prepared this time. The document itself is fine — its text,
        search and answers are not affected.
      </p>
    );
  }
  // UNAVAILABLE: say why, in the reader's terms.
  const reason =
    document.status === "QUARANTINED"
      ? "Quarantined files are not read, so there is no brief."
      : document.status === "FAILED"
        ? "There is no brief because the document could not be processed."
        : IN_FLIGHT.includes(document.status)
          ? "The brief appears once processing has finished."
          : "There is no brief for this document.";
  return <p className="muted">{reason}</p>;
}

function SpanLinkText({ documentId, span, children }: {
  documentId: number;
  span: { page_number: number; start: number; end: number };
  children: ReactNode;
}) {
  return (
    <Link
      to={spanLink(documentId, span)}
      className="span-link"
      title={`Show on page ${span.page_number}`}
    >
      {children}
    </Link>
  );
}

function BriefCard({ document, insights }: { document: DocumentDetail; insights: DocumentInsights }) {
  const ready = insights.status === "READY";
  const health = ready ? healthLine(insights.stats) : "";
  return (
    <Card
      title="Brief"
      icon="sparkles"
      className="brief-card"
      description={health || undefined}
      actions={
        ready ? (
          <Badge tone="neutral" icon="quote" title="Sentences chosen from the document, not written by AI">
            Extracted from the document
          </Badge>
        ) : null
      }
    >
      {!ready ? (
        <BriefState insights={insights} document={document} />
      ) : insights.summary.length ? (
        <ol className="brief-summary">
          {insights.summary.map((sentence) => (
            <li key={`${sentence.page_number}:${sentence.start}`}>
              <SpanLinkText documentId={document.id} span={sentence}>
                {sentence.text.replace(/\s+/g, " ")}
              </SpanLinkText>
              <span className="brief-page">p. {sentence.page_number}</span>
            </li>
          ))}
        </ol>
      ) : (
        <p className="muted">
          No sentence here reads as a summary — this looks like a form, a table or a list. The
          key facts below are still taken from it.
        </p>
      )}
    </Card>
  );
}

function KeyPointsCard({ document, insights }: { document: DocumentDetail; insights: DocumentInsights }) {
  if (insights.status !== "READY" || !insights.key_points.length) return null;
  return (
    <Card title="Key points" icon="list-checks">
      <ul className="key-points">
        {insights.key_points.map((point) => (
          <li key={`${point.page_number}:${point.start}`}>
            <Badge tone={KIND_TONE[point.kind] ?? "neutral"}>
              {KIND_LABEL[point.kind] ?? humanize(point.kind)}
            </Badge>
            <SpanLinkText documentId={document.id} span={point}>
              {point.text.replace(/\s+/g, " ")}
            </SpanLinkText>
          </li>
        ))}
      </ul>
    </Card>
  );
}

function QuestionsCard({ document, insights }: { document: DocumentDetail; insights: DocumentInsights }) {
  const navigate = useNavigate();
  if (insights.status !== "READY" || !insights.suggested_questions.length) return null;
  const askable = document.status === "COMPLETED" && !document.is_archived;
  return (
    <Card
      title="Questions this document answers"
      icon="message"
      description="Each one was checked against the document's own text."
    >
      <div className="question-chips">
        {insights.suggested_questions.map((question) => (
          <button
            key={question}
            type="button"
            className="question-chip"
            disabled={!askable}
            title={askable ? "Ask this about this document" : "Available while the document is searchable"}
            onClick={() =>
              navigate("/ask", {
                state: { documentId: document.id, documentTitle: document.title, question },
              })
            }
          >
            <Icon name="sparkles" size={14} />
            {question}
          </button>
        ))}
      </div>
    </Card>
  );
}

function factText(entity: InsightEntity): string {
  if (entity.type === "DEADLINE" || entity.type === "DATE") {
    return formatDate(entity.normalized) ?? entity.text;
  }
  return entity.text.replace(/\s+/g, " ");
}

function KeyFactsCard({ document, insights }: { document: DocumentDetail; insights: DocumentInsights }) {
  if (insights.status !== "READY" || !insights.entities.length) return null;
  const groups = FACT_GROUPS.map((group) => ({
    ...group,
    entities: insights.entities.filter((entity) => group.types.includes(entity.type)),
  })).filter((group) => group.entities.length);
  return (
    <Card title="Key facts" icon="zap">
      <div className="fact-groups">
        {groups.map((group) => (
          <section key={group.title} className="fact-group" aria-label={group.title}>
            <h3 className="fact-group-title">
              <Icon name={group.icon} size={14} />
              {group.title}
            </h3>
            <ul className="fact-chips">
              {group.entities.slice(0, 12).map((entity) => (
                <li key={`${entity.type}:${entity.page_number}:${entity.start}`}>
                  <Link
                    to={spanLink(document.id, entity)}
                    className={`fact-chip${entity.type === "DEADLINE" ? " fact-chip-deadline" : ""}`}
                    title={[entity.label, entity.normalized, `page ${entity.page_number}`]
                      .filter(Boolean)
                      .join(" · ")}
                  >
                    {entity.type === "DEADLINE" ? <span className="fact-chip-kind">Due</span> : null}
                    <span className="fact-chip-text">{factText(entity)}</span>
                    {entity.label && (entity.type === "DEADLINE" || entity.type === "DATE") ? (
                      <span className="fact-chip-label">{entity.label}</span>
                    ) : null}
                    {entity.count > 1 ? <span className="fact-chip-count">×{entity.count}</span> : null}
                  </Link>
                </li>
              ))}
            </ul>
          </section>
        ))}
      </div>
    </Card>
  );
}

function TopicsCard({ insights }: { insights: DocumentInsights }) {
  if (insights.status !== "READY" || !insights.key_phrases.length) return null;
  return (
    <Card title="Topics" icon="hash">
      <ul className="topic-chips">
        {insights.key_phrases.map((phrase) => (
          <li key={phrase.text}>
            <Link to="/search" state={{ query: phrase.text }} className="topic-chip" title="Search for this">
              {phrase.text}
            </Link>
          </li>
        ))}
      </ul>
    </Card>
  );
}

function RelatedCard({ document }: { document: DocumentDetail }) {
  const ready = document.status === "COMPLETED";
  const related = useResource<RelatedDocuments>(
    () => insightsApi.related(document.id),
    [document.id, document.current_version?.id ?? null],
    { enabled: ready },
  );
  if (!ready) return null;
  return (
    <Card
      title="Related documents"
      icon="layers"
      description={
        related.data?.method === "lexical"
          ? "By shared key phrases"
          : related.data?.method === "semantic"
            ? "By meaning"
            : undefined
      }
    >
      {related.loading && !related.data ? (
        <Skeleton lines={2} />
      ) : related.error && !related.data ? (
        <p className="muted small">
          Related documents could not be loaded.{" "}
          <button type="button" className="link-button" onClick={related.reload}>
            Try again
          </button>
        </p>
      ) : related.data?.results.length ? (
        <ul className="related-list">
          {related.data.results.map((row) => (
            <li key={row.id}>
              <Link to={`/documents/${row.id}`} className="related-title">
                {row.title}
              </Link>
              <span className="related-meta">
                {TYPE_LABEL[row.document_type] ?? humanize(row.document_type)}
                {" · "}
                {formatPercent(row.similarity)} similar
              </span>
              {row.near_duplicate ? (
                <Badge tone="warning" icon="copy" title="The same or nearly the same content">
                  Possible duplicate
                </Badge>
              ) : null}
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted small">No other document you can read is closely related.</p>
      )}
    </Card>
  );
}

function layoutLabel(results: ClassificationResult[] | null): string | null {
  const layout = results?.find((result) => result.taxonomy === "layout_class");
  if (!layout) return null;
  if (layout.abstained || layout.predicted_label === "unknown") return "Unclear";
  return `${humanize(layout.predicted_label)} (${formatPercent(layout.confidence)})`;
}

function DetailsCard({ document, onEditType }: { document: DocumentDetail; onEditType?: () => void }) {
  const classification = useResource(
    () => api.classification(document.id),
    [document.id, document.status],
  );
  const provenance = typeProvenance(document);
  const layout = layoutLabel(classification.data);
  const version = document.current_version;
  return (
    <Card title="Details" icon="info">
      <DescriptionList
        items={[
          [
            "Type",
            <span key="t" className="type-provenance">
              <span>{TYPE_LABEL[document.document_type]}</span>
              {provenance ? <span className="muted small">{provenance}</span> : null}
              {document.can_manage && onEditType ? (
                <button type="button" className="link-button small" onClick={onEditType}>
                  Change
                </button>
              ) : null}
            </span>,
          ],
          ...(layout ? ([["Layout", layout]] as [ReactNode, ReactNode][]) : []),
          ["Department", document.department || "None (visible to the owner only)"],
          ["Owner", document.owner_username || "—"],
          ...(document.collections?.length
            ? ([
                [
                  "Collections",
                  <span key="c" className="badge-row">
                    {document.collections.map((collection) => (
                      <Badge key={collection.id} tone="neutral" icon="folder">
                        {collection.name}
                      </Badge>
                    ))}
                  </span>,
                ],
              ] as [ReactNode, ReactNode][])
            : []),
          ...(version
            ? ([
                [
                  "File",
                  `${version.file_name} · v${version.version_number} · ${formatBytes(version.file_size)}`,
                ],
                [
                  "Uploaded",
                  `${formatDateTime(version.created_at)}${
                    version.uploaded_by_name ? ` by ${version.uploaded_by_name}` : ""
                  }`,
                ],
              ] as [ReactNode, ReactNode][])
            : []),
          ["Last updated", formatDateTime(document.updated_at)],
          ...(document.is_archived
            ? ([["Archived", formatDateTime(document.archived_at)]] as [ReactNode, ReactNode][])
            : []),
        ]}
      />
    </Card>
  );
}

/** The document's Overview tab. */
export function InsightsOverview({
  document,
  onEditType,
}: {
  document: DocumentDetail;
  onEditType?: () => void;
}) {
  const insights = useResource(
    () => insightsApi.insights(document.id),
    [document.id, document.status, document.document_type, document.current_version?.id ?? null],
  );
  // The brief is written moments after a document is ready. Until then the
  // card says so and checks back - only while the page is in view.
  usePolling(insights.reload, insights.data?.status === "PENDING", 4000);

  const brief = insights.data;
  return (
    <div className="insights-layout">
      <div className="insights-main stack-lg">
        {insights.loading && !brief ? (
          <Card title="Brief" icon="sparkles">
            <Skeleton lines={4} />
          </Card>
        ) : insights.error && !brief ? (
          <Card title="Brief" icon="sparkles">
            <ErrorState
              error={insights.error}
              onRetry={insights.reload}
              title="The brief could not be loaded"
            />
          </Card>
        ) : brief ? (
          <>
            <BriefCard document={document} insights={brief} />
            <KeyPointsCard document={document} insights={brief} />
            <QuestionsCard document={document} insights={brief} />
          </>
        ) : null}
      </div>
      <div className="insights-aside stack-lg">
        <DetailsCard document={document} onEditType={onEditType} />
        {brief ? <KeyFactsCard document={document} insights={brief} /> : null}
        {brief ? <TopicsCard insights={brief} /> : null}
        <RelatedCard document={document} />
      </div>
    </div>
  );
}


