/**
 * Analytics for administrators: what the platform has been doing, from its
 * own records (GET /api/v1/admin/analytics/). Department administrators
 * see their department; system administrators see everything. Range and
 * tab live in the URL, so a view can be bookmarked or shared.
 */
import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { analyticsApi, type AnalyticsRange, type AnalyticsReport, type Kpi } from "../../api/analytics";
import { ApiError } from "../../api/client";
import { useResource } from "../../api/hooks";
import {
  BarChart,
  BarList,
  Donut,
  KpiTile,
  LineChart,
  type DeltaStyle,
  type GoodDirection,
  type ValueFormat,
} from "../../components/charts";
import type { IconName } from "../../components/icons";
import {
  Alert,
  Button,
  Card,
  ErrorState,
  PageHeader,
  TabPanel,
  Tabs,
  errorMessage,
  useDocumentTitle,
  useToast,
} from "../../components/ui";
import { formatBytes, formatDuration, formatNumber, formatPercent, formatRelative } from "../../lib/format";
import { FILE_KIND_LABEL, STAGE_LABEL, TYPE_LABEL, errorCodeLabel, humanize } from "../../lib/labels";
import "../../styles/analytics.css";
import { QualityTab } from "./AnalyticsQuality";

const RANGES: [AnalyticsRange, string][] = [
  ["7d", "7 days"],
  ["30d", "30 days"],
  ["90d", "90 days"],
];

const TABS = [
  { id: "overview", label: "Overview", icon: "dashboard" as IconName },
  { id: "quality", label: "Answers & quality", icon: "sparkles" as IconName },
  { id: "documents", label: "Documents", icon: "files" as IconName },
  { id: "usage", label: "Usage", icon: "users" as IconName },
];

const ratio: ValueFormat = (value) => formatPercent(value, value > 0 && value < 0.1 ? 1 : 0);
const seconds: ValueFormat = (value) => formatDuration(value * 1000);

/** "Now" that moves on every 30 s, so "updated 3 minutes ago" stays true. */
function useNow(intervalMs = 30_000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs]);
  return now;
}

function Tile({
  label,
  kpi,
  format = formatNumber,
  style = "percent",
  good = "up",
  period,
  icon,
  color,
  loading,
}: {
  label: string;
  kpi: Kpi | undefined;
  format?: ValueFormat;
  style?: DeltaStyle;
  good?: GoodDirection;
  period: string;
  icon?: IconName;
  color?: 1 | 2 | 3;
  loading: boolean;
}) {
  return (
    <KpiTile
      label={label}
      icon={icon}
      loading={loading || !kpi}
      value={kpi?.value ?? null}
      format={format}
      color={color}
      delta={kpi ? { value: kpi.value, previous: kpi.previous, style, good, period } : undefined}
      series={kpi?.series}
    />
  );
}

function isEmpty(report: AnalyticsReport): boolean {
  const { kpis } = report;
  return [kpis.questions, kpis.documents_processed, kpis.documents_failed, kpis.active_users, kpis.searches].every(
    (kpi) => !kpi.value && !kpi.previous,
  );
}

function Overview({ report, loading, period }: { report: AnalyticsReport | null; loading: boolean; period: string }) {
  const kpis = report?.kpis;
  const dates = report?.questions.series.map((point) => point.date) ?? [];
  const busy = loading && !report;
  return (
    <div className="stack">
      <div className="analytics-kpis">
        <Tile label="Questions asked" icon="message" kpi={kpis?.questions} period={period} loading={busy} />
        <Tile
          label="Answered from documents"
          icon="check-circle"
          kpi={kpis?.answer_rate}
          format={ratio}
          style="points"
          period={period}
          loading={busy}
        />
        <Tile
          label="Rated helpful"
          icon="thumbs-up"
          kpi={kpis?.helpful_rate}
          format={ratio}
          style="points"
          period={period}
          loading={busy}
        />
        <Tile label="Active people" icon="users" kpi={kpis?.active_users} period={period} loading={busy} />
        <Tile label="Documents processed" icon="files" kpi={kpis?.documents_processed} period={period} loading={busy} />
        <Tile
          label="Processing failures"
          icon="alert-triangle"
          kpi={kpis?.documents_failed}
          good="down"
          period={period}
          color={2}
          loading={busy}
        />
        <Tile label="Searches" icon="search" kpi={kpis?.searches} period={period} loading={busy} />
        <Tile
          label="Searches with no results"
          icon="filter"
          kpi={kpis?.zero_hit_rate}
          format={ratio}
          style="points"
          good="down"
          period={period}
          color={2}
          loading={busy}
        />
      </div>
      <Card
        title="Questions"
        icon="message"
        description="Every question, by how it was answered: from the documents, from general knowledge, or not at all."
      >
        <BarChart
          stacked
          integer
          title="Questions per day by outcome"
          labels={dates}
          loading={busy}
          height={240}
          series={[
            { key: "answered", label: "Answered from documents", values: report?.questions.series.map((p) => p.answered) ?? [] },
            { key: "abstained", label: "Not answered", values: report?.questions.series.map((p) => p.abstained) ?? [] },
            { key: "general", label: "General knowledge", values: report?.questions.series.map((p) => p.general) ?? [] },
          ]}
          empty="No questions were asked in this period."
        />
      </Card>
      <div className="analytics-grid">
        <Card
          title="Answer time"
          icon="clock"
          description={
            report?.latency.overall.p50_ms
              ? `Typically ${formatDuration(report.latency.overall.p50_ms)}; the slowest 5% took ${formatDuration(report.latency.overall.p95_ms)} or more.`
              : "How long people waited for an answer."
          }
        >
          <LineChart
            title="Answer time per day"
            labels={dates}
            loading={busy}
            format={formatDuration}
            series={[
              { key: "p50", label: "Typical (median)", values: report?.latency.series.map((p) => p.p50_ms) ?? [] },
              { key: "p95", label: "Slowest 5%", values: report?.latency.series.map((p) => p.p95_ms) ?? [] },
            ]}
            empty="No answers were timed in this period."
          />
        </Card>
        <Card title="Document processing" icon="activity" description="Processing runs that finished each day.">
          <BarChart
            stacked
            integer
            title="Processing runs per day"
            labels={dates}
            loading={busy}
            series={[
              { key: "completed", label: "Completed", values: report?.documents.processing.map((p) => p.completed) ?? [] },
              { key: "failed", label: "Failed", values: report?.documents.processing.map((p) => p.failed) ?? [], color: 2 },
            ]}
            empty="No documents were processed in this period."
          />
        </Card>
      </div>
    </div>
  );
}

function DocumentsTab({ report, loading, period }: { report: AnalyticsReport | null; loading: boolean; period: string }) {
  const busy = loading && !report;
  const documents = report?.documents;
  const dates = documents?.uploads.map((point) => point.date) ?? [];
  const uploads = documents?.uploads.reduce((sum, point) => sum + point.count, 0) ?? 0;
  const allDepartments = report?.scope.department === null;
  return (
    <div className="stack">
      <div className="analytics-kpis">
        <KpiTile label="Files uploaded" icon="upload" loading={busy} value={report ? uploads : null} series={documents?.uploads.map((p) => p.count)} caption="New documents and new versions" />
        <Tile label="Documents processed" icon="files" kpi={report?.kpis.documents_processed} period={period} loading={busy} />
        <Tile label="Processing failures" icon="alert-triangle" kpi={report?.kpis.documents_failed} good="down" color={2} period={period} loading={busy} />
        <Tile label="Storage used" icon="database" kpi={report?.kpis.storage_bytes} format={formatBytes} good="neutral" period="growth in this period" loading={busy} />
      </div>
      <div className="analytics-grid">
        <Card title="Throughput" icon="activity" description="Processing runs that finished each day, completed or failed.">
          <BarChart
            grouped
            integer
            title="Completed and failed runs per day"
            labels={dates}
            loading={busy}
            series={[
              { key: "completed", label: "Completed", values: documents?.processing.map((p) => p.completed) ?? [] },
              { key: "failed", label: "Failed", values: documents?.processing.map((p) => p.failed) ?? [], color: 2 },
            ]}
            empty="No documents were processed in this period."
          />
        </Card>
        <Card title="Uploads" icon="upload" description="Files uploaded each day.">
          <BarChart
            integer
            title="Files uploaded per day"
            labels={dates}
            loading={busy}
            series={[{ key: "uploads", label: "Files uploaded", values: documents?.uploads.map((p) => p.count) ?? [] }]}
            empty="Nothing was uploaded in this period."
          />
        </Card>
        <Card title="Why processing failed" icon="alert-triangle" description="Failed runs in this period, by reason.">
          <BarList
            title="Processing failures by reason"
            loading={busy}
            color={2}
            items={(documents?.failures ?? []).map((row) => ({
              key: row.error_code || "unknown",
              label: errorCodeLabel(row.error_code),
              value: row.count,
            }))}
            empty="No processing failures in this period."
          />
          {documents?.failures.length ? (
            <p className="analytics-footnote">
              <Link to="/admin/processing">Open Processing</Link> to retry failed runs.
            </p>
          ) : null}
        </Card>
        <Card title="Stage durations" icon="clock" description="How long each pipeline stage takes, over the stages that ran.">
          {documents && documents.stages.length ? (
            <div className="table-wrap analytics-table-wrap">
              <table className="table analytics-stage-table">
                <thead>
                  <tr>
                    <th scope="col">Stage</th>
                    <th scope="col">Typical</th>
                    <th scope="col">Slowest 5%</th>
                    <th scope="col">Runs</th>
                  </tr>
                </thead>
                <tbody>
                  {documents.stages.map((stage) => (
                    <tr key={stage.stage}>
                      <th scope="row">{STAGE_LABEL[stage.stage as keyof typeof STAGE_LABEL] ?? humanize(stage.stage)}</th>
                      <td>{seconds(stage.p50_seconds)}</td>
                      <td>{seconds(stage.p95_seconds)}</td>
                      <td>{formatNumber(stage.runs)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <BarList title="Stage durations" loading={busy} items={[]} empty="No pipeline stages ran in this period." />
          )}
        </Card>
        <Card title="Library by type" icon="layers" description="Documents in the library now, archived ones aside.">
          <Donut
            title="Documents by type"
            loading={busy}
            centerLabel="documents"
            items={(documents?.by_type ?? []).map((row) => ({
              key: row.document_type,
              label: TYPE_LABEL[row.document_type] ?? humanize(row.document_type),
              value: row.count,
            }))}
            empty="No documents yet."
          />
        </Card>
        <Card title="Storage by file kind" icon="database" description={documents ? `${formatBytes(documents.storage.total_bytes)} in ${formatNumber(documents.storage.files)} files, every version kept.` : undefined}>
          <Donut
            title="Storage by file kind"
            loading={busy}
            format={formatBytes}
            centerLabel="stored"
            items={(documents?.storage.by_kind ?? []).map((row) => ({
              key: row.kind,
              label: FILE_KIND_LABEL[row.kind] ?? humanize(row.kind),
              value: row.bytes,
            }))}
            empty="Nothing stored yet."
          />
        </Card>
        {allDepartments ? (
          <>
            <Card title="Library by department" icon="building">
              <BarList
                title="Documents by department"
                loading={busy}
                items={(documents?.by_department ?? []).map((row) => ({
                  key: row.department || "—",
                  label: row.department || "No department",
                  value: row.count,
                }))}
                empty="No documents yet."
              />
            </Card>
            <Card title="Storage by department" icon="database">
              <BarList
                title="Storage by department"
                loading={busy}
                format={formatBytes}
                items={(documents?.storage.by_department ?? []).map((row) => ({
                  key: row.department || "—",
                  label: row.department || "No department",
                  hint: `${formatNumber(row.files)} files`,
                  value: row.bytes,
                }))}
                empty="Nothing stored yet."
              />
            </Card>
          </>
        ) : null}
      </div>
    </div>
  );
}

function UsageTab({ report, loading, period }: { report: AnalyticsReport | null; loading: boolean; period: string }) {
  const busy = loading && !report;
  const usage = report?.usage;
  const dates = usage?.active_users.map((point) => point.date) ?? [];
  return (
    <div className="stack">
      <div className="analytics-kpis analytics-kpis-3">
        <Tile label="Active people" icon="users" kpi={report?.kpis.active_users} period={period} loading={busy} />
        <Tile label="Searches" icon="search" kpi={report?.kpis.searches} period={period} loading={busy} />
        <Tile label="Searches with no results" icon="filter" kpi={report?.kpis.zero_hit_rate} format={ratio} style="points" good="down" color={2} period={period} loading={busy} />
      </div>
      <div className="analytics-grid">
        <Card title="Active people" icon="users" description="People who signed in, opened a document, searched or asked, each day.">
          <LineChart
            area
            integer
            title="Active people per day"
            labels={dates}
            loading={busy}
            series={[{ key: "active", label: "Active people", values: usage?.active_users.map((p) => p.count) ?? [] }]}
            empty="No one was active in this period."
          />
        </Card>
        <Card title="Searches" icon="search" description="Searches each day, and how many found nothing. Search words are never recorded.">
          <BarChart
            stacked
            integer
            title="Searches per day"
            labels={dates}
            loading={busy}
            series={[
              { key: "found", label: "Found results", values: usage?.searches.map((p) => p.count - p.zero_hit) ?? [] },
              { key: "zero", label: "No results", values: usage?.searches.map((p) => p.zero_hit) ?? [] },
            ]}
            empty="No searches in this period."
          />
        </Card>
        <Card title="Most cited documents" icon="quote" description="The documents answers relied on, by how many answers cited them.">
          <BarList
            title="Most cited documents"
            loading={busy}
            items={(usage?.top_cited ?? []).map((row) => ({
              key: String(row.document_id),
              label: row.title,
              href: `/documents/${row.document_id}`,
              hint: `${formatNumber(row.citations)} ${row.citations === 1 ? "citation" : "citations"}`,
              value: row.answers,
            }))}
            empty="No answer has cited a document in this period."
          />
        </Card>
        <Card title="Most read documents" icon="eye" description="By how many people opened them; visits count one per person per day.">
          <BarList
            title="Most read documents"
            loading={busy}
            color={3}
            items={(usage?.most_viewed ?? []).map((row) => ({
              key: String(row.document_id),
              label: row.title,
              href: `/documents/${row.document_id}`,
              hint: `${formatNumber(row.visits)} ${row.visits === 1 ? "visit" : "visits"}`,
              value: row.readers,
            }))}
            empty="No documents were opened in this period."
          />
        </Card>
      </div>
    </div>
  );
}

export default function AnalyticsPage() {
  useDocumentTitle("Analytics");
  const toast = useToast();
  const now = useNow();
  const [params, setParams] = useSearchParams();
  const range = (RANGES.find(([key]) => key === params.get("range"))?.[0] ?? "30d") as AnalyticsRange;
  const tab = TABS.find((item) => item.id === params.get("tab"))?.id ?? "overview";
  const report = useResource(() => analyticsApi.report(range), [range]);
  // The period on screen now, for a refresh to check when it comes back.
  const shown = useRef(range);
  shown.current = range;
  const [refreshing, setRefreshing] = useState(false);
  const data = report.data;
  const days = RANGES.find(([key]) => key === range)?.[1] ?? "";
  const period = `vs previous ${days}`;

  const update = (changes: Record<string, string>) => {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(changes)) next.set(key, value);
    setParams(next, { replace: true });
  };

  const refresh = async () => {
    const requested = range;
    setRefreshing(true);
    try {
      const recomputed = await analyticsApi.report(requested, true);
      // Only onto the period it was asked for. The recompute is slow, and
      // another period chosen meanwhile (a button, the address) has its own
      // report on screen by now: setData would put these figures under that
      // label, and remember them as that period's.
      if (shown.current === requested) report.setData(recomputed);
    } catch (caught) {
      toast(
        caught instanceof ApiError && caught.status === 429
          ? "Refreshed several times already - the figures shown are current to the time above. Try again in a few minutes."
          : errorMessage(caught),
        "danger",
      );
    } finally {
      setRefreshing(false);
    }
  };

  const scope = data ? (data.scope.department ? `${data.scope.department} department` : "All departments") : "";

  return (
    <div className="page analytics-page">
      <PageHeader
        title="Analytics"
        description={
          <>
            Questions, answer quality, documents and usage, computed from the platform's own records.
            {scope ? <span className="analytics-scope"> {scope}.</span> : null} No question or search text is used.
          </>
        }
        actions={
          <div className="analytics-actions">
            <div className="segmented" role="group" aria-label="Period">
              {RANGES.map(([key, label]) => (
                <button
                  key={key}
                  type="button"
                  className={`segment${range === key ? " active" : ""}`}
                  aria-pressed={range === key}
                  onClick={() => update({ range: key })}
                >
                  {label}
                </button>
              ))}
            </div>
            <Button icon="refresh" onClick={refresh} loading={refreshing} disabled={!data}>
              Refresh
            </Button>
          </div>
        }
      />
      {data ? (
        <p className="analytics-updated" aria-live="polite">
          Updated {formatRelative(data.generated_at, now)}
          {data.range.time_zone !== "UTC" ? ` · days in ${data.range.time_zone}` : " · days in UTC"}
        </p>
      ) : null}

      {report.error && !data ? (
        <ErrorState error={report.error} onRetry={report.reload} />
      ) : (
        <>
          {data && isEmpty(data) ? (
            <Alert
              tone="info"
              title="Nothing has happened in this period yet"
              action={
                <Link to="/documents" className="btn btn-secondary btn-sm">
                  Go to documents
                </Link>
              }
            >
              These charts fill in as people upload documents, search and ask questions. Try a longer period,
              or come back once the library is in use.
            </Alert>
          ) : null}
          <Tabs tabs={TABS} selected={tab} onSelect={(id) => update({ tab: id })} label="Analytics sections" />
          <TabPanel id={tab}>
            {tab === "overview" ? (
              <Overview report={data} loading={report.loading} period={period} />
            ) : tab === "quality" ? (
              <QualityTab report={data} loading={report.loading && !data} />
            ) : tab === "documents" ? (
              <DocumentsTab report={data} loading={report.loading} period={period} />
            ) : (
              <UsageTab report={data} loading={report.loading} period={period} />
            )}
          </TabPanel>
        </>
      )}
    </div>
  );
}
