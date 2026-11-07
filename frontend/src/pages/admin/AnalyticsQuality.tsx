/**
 * The "Answers & quality" tab of the Analytics page: how answers turned
 * out, why some were withheld, what people said about them (the review
 * queue), and the offline benchmark runs.
 */
import { useState } from "react";
import { Link } from "react-router-dom";

import { analyticsApi, type AnalyticsReport, type EvaluationRun, type FeedbackItem } from "../../api/analytics";
import { useResource } from "../../api/hooks";
import type { FeedbackRating } from "../../api/types";
import { BarChart, BarList, KpiTile, LineChart, type GoodDirection } from "../../components/charts";
import { Icon } from "../../components/icons";
import { Badge, Card, EmptyState, ErrorState, Pagination, Skeleton } from "../../components/ui";
import { formatDuration, formatPercent, formatRelative } from "../../lib/format";
import { ABSTENTION_LABEL, ANSWER_STAGE_LABEL, explainAbstention, humanize } from "../../lib/labels";

const RATING: Record<FeedbackRating, { label: string; tone: "success" | "warning" | "danger"; icon: "thumbs-up" | "thumbs-down" | "flag" }> = {
  HELPFUL: { label: "Helpful", tone: "success", icon: "thumbs-up" },
  NOT_HELPFUL: { label: "Not helpful", tone: "warning", icon: "thumbs-down" },
  INCORRECT: { label: "Incorrect", tone: "danger", icon: "flag" },
};

const FEEDBACK_VIEWS: [string, string, string][] = [
  ["flagged", "Flagged", "INCORRECT,NOT_HELPFUL"],
  ["incorrect", "Incorrect", "INCORRECT"],
  ["all", "All ratings", ""],
];

const ratio = (value: number) => formatPercent(value, value > 0 && value < 0.1 ? 1 : 0);

function FeedbackRow({ item }: { item: FeedbackItem }) {
  const rating = RATING[item.rating];
  return (
    <li className="feedback-item">
      <div className="feedback-item-head">
        <Badge tone={rating.tone} icon={rating.icon}>
          {rating.label}
        </Badge>
        <span className="muted small">{formatRelative(item.created_at)}</span>
        {item.department ? <span className="muted small">· {item.department}</span> : null}
        <span className="feedback-item-meta">
          {item.abstained ? (
            <Badge tone="muted">Not answered</Badge>
          ) : item.source === "general" ? (
            <Badge tone="warning" icon="globe">
              General knowledge
            </Badge>
          ) : (
            <Badge tone={item.generative ? "info" : "neutral"} icon={item.generative ? "sparkles" : "quote"}>
              {item.generative ? "Written from sources" : "Quoted"}
            </Badge>
          )}
        </span>
      </div>
      {item.question !== null ? (
        <p className="feedback-question">
          <span className="feedback-caption">Question</span>
          {item.question || <span className="muted">Not found</span>}
        </p>
      ) : null}
      {item.answer_withheld ? (
        <p className="feedback-withheld">
          <Icon name="lock" size={14} />
          The answer quotes a document outside your department, so it is not shown here.
        </p>
      ) : (
        <blockquote className="feedback-answer">{item.answer_excerpt || "—"}</blockquote>
      )}
      {item.comment ? (
        <p className="feedback-comment">
          <span className="feedback-caption">Comment</span>“{item.comment}”
        </p>
      ) : null}
      <div className="feedback-item-foot">
        <span>
          {item.citations === 1 ? "1 citation" : `${item.citations} citations`}
          {item.model ? ` · ${item.model}` : ""}
          {item.abstained && item.abstention_reason ? ` · ${humanize(item.abstention_reason)}` : ""}
        </span>
        {item.documents.length ? (
          <span className="feedback-sources">
            {item.documents.map((document) => (
              <Link key={document.id} to={`/documents/${document.id}`} className="feedback-source">
                <Icon name="file-text" size={13} />
                {document.title}
              </Link>
            ))}
          </span>
        ) : null}
      </div>
    </li>
  );
}

function FeedbackQueue({ systemScope }: { systemScope: boolean }) {
  const [view, setView] = useState("flagged");
  const [page, setPage] = useState(1);
  const rating = FEEDBACK_VIEWS.find(([id]) => id === view)?.[2] ?? "";
  const feedback = useResource(() => analyticsApi.feedback({ rating, page }), [rating, page]);

  return (
    <Card
      title="Rated answers"
      icon="flag"
      description={
        systemScope
          ? "What people said about answers, newest first. The question is shown for flagged answers only; who rated is never shown."
          : "What people in your department said about answers, newest first. Questions are visible to system administrators only, and who rated is never shown."
      }
      actions={
        <div className="segmented" role="group" aria-label="Ratings to show">
          {FEEDBACK_VIEWS.map(([id, label]) => (
            <button
              key={id}
              type="button"
              className={`segment${view === id ? " active" : ""}`}
              aria-pressed={view === id}
              onClick={() => {
                setView(id);
                setPage(1);
              }}
            >
              {label}
            </button>
          ))}
        </div>
      }
    >
      {feedback.error && !feedback.data ? (
        <ErrorState error={feedback.error} onRetry={feedback.reload} />
      ) : !feedback.data ? (
        <Skeleton lines={5} />
      ) : !feedback.data.results.length ? (
        <EmptyState title={view === "all" ? "No ratings yet" : "Nothing flagged"} icon="check-circle">
          {view === "all"
            ? "When people rate answers with the buttons under each one, their ratings appear here."
            : "No one has marked an answer incorrect or unhelpful. Ratings appear here as people give them."}
        </EmptyState>
      ) : (
        <>
          <ul className="feedback-list">
            {feedback.data.results.map((item) => (
              <FeedbackRow key={item.id} item={item} />
            ))}
          </ul>
          <Pagination page={page} count={feedback.data.count} onPage={setPage} />
        </>
      )}
    </Card>
  );
}

// --- Benchmark runs ---------------------------------------------------------------

interface Scorecard {
  key: string;
  label: string;
  value: (run: EvaluationRun) => number | undefined;
  /** A share (0-1), printed as a percentage; otherwise a 0-1 score. */
  percent: boolean;
  good: GoodDirection;
}

const SCORECARD: Scorecard[] = [
  // The runner scores k = 1, 3, 5 and 10 (evaluation.runner.DEFAULT_KS).
  {
    key: "recall",
    label: "Recall@5",
    value: (run) => run.metrics.retrieval?.recall_at_k?.["5"],
    percent: true,
    good: "up",
  },
  {
    key: "ndcg",
    label: "nDCG@10",
    value: (run) => run.metrics.retrieval?.ndcg_at_k?.["10"],
    percent: false,
    good: "up",
  },
  { key: "mrr", label: "MRR", value: (run) => run.metrics.retrieval?.mrr, percent: false, good: "up" },
  {
    key: "groundedness",
    label: "Groundedness",
    value: (run) => run.metrics.generation?.groundedness,
    percent: true,
    good: "up",
  },
  {
    key: "citation_accuracy",
    label: "Citation accuracy",
    value: (run) => run.metrics.generation?.citation_accuracy,
    percent: true,
    good: "up",
  },
  {
    key: "abstention_accuracy",
    label: "Abstention accuracy",
    value: (run) => run.metrics.generation?.abstention_accuracy,
    percent: true,
    good: "up",
  },
];

const score = (value: number) => value.toFixed(2);

function componentName(configuration: Record<string, unknown>, part: string): string {
  const value = configuration[part];
  return value && typeof value === "object" && typeof (value as { name?: unknown }).name === "string"
    ? (value as { name: string }).name
    : "—";
}

function Benchmarks() {
  const runs = useResource(() => analyticsApi.evaluationRuns(), []);
  if (runs.error && !runs.data) return <ErrorState error={runs.error} onRetry={runs.reload} title="Benchmark runs could not be loaded" />;
  if (!runs.data) return <Skeleton lines={4} />;

  // The API lists newest first.
  const completed = runs.data.results.filter((run) => run.status === "COMPLETED");
  const latest = completed[0];
  const previous = completed[1];
  const trend = [...completed].slice(0, 20).reverse();

  if (!runs.data.results.length) {
    return (
      <Card title="Benchmark" icon="gauge">
        <EmptyState title="No benchmark runs yet" icon="gauge">
          A benchmark replays a fixed set of questions with known answers and scores retrieval, grounding,
          citations and abstention, so a change to the system can be measured rather than guessed. An
          operator starts a run with the evaluate management command; its scores appear here.
        </EmptyState>
      </Card>
    );
  }

  return (
    <div className="stack">
      {latest ? (
        <Card
          title="Latest benchmark"
          icon="gauge"
          description={`${latest.dataset.name} ${latest.dataset.version} · ${latest.cases_scored} of ${latest.cases_total} cases · ${formatRelative(latest.completed_at ?? latest.started_at)}${previous ? " · change against the run before" : ""}`}
        >
          <div className="analytics-kpis analytics-kpis-compact">
            {SCORECARD.map((metric) => {
              const value = metric.value(latest);
              const before = previous ? metric.value(previous) : undefined;
              return (
                <KpiTile
                  key={metric.key}
                  label={metric.label}
                  value={value ?? null}
                  format={metric.percent ? ratio : score}
                  delta={
                    previous && value !== undefined && before !== undefined
                      ? { value, previous: before, style: metric.percent ? "points" : "absolute", good: metric.good }
                      : undefined
                  }
                />
              );
            })}
          </div>
        </Card>
      ) : null}
      {trend.length > 1 ? (
        <Card title="Benchmark trend" icon="activity" description="Each completed run, oldest to newest.">
          <LineChart
            title="Benchmark scores by run"
            labelType="category"
            labels={trend.map((run) => run.label || `Run ${run.id}`)}
            yMax={1}
            format={ratio}
            axisFormat={(value) => formatPercent(value)}
            series={SCORECARD.filter((metric) => metric.percent).map((metric) => ({
              key: metric.key,
              label: metric.label,
              values: trend.map((run) => metric.value(run) ?? null),
            }))}
          />
        </Card>
      ) : null}
      <Card title="Benchmark runs" icon="list-checks" padded={false}>
        <div className="table-wrap analytics-table-wrap">
          <table className="table table-stack">
            <thead>
              <tr>
                <th scope="col">Run</th>
                <th scope="col">Status</th>
                <th scope="col">Cases</th>
                <th scope="col">Answerer</th>
                <th scope="col">Reranker</th>
                <th scope="col">Took</th>
              </tr>
            </thead>
            <tbody>
              {runs.data.results.map((run) => (
                <tr key={run.id}>
                  <td>
                    <span className="analytics-run-title">{run.label || `Run ${run.id}`}</span>
                    <span className="table-sub">
                      {run.dataset.name} {run.dataset.version} · {formatRelative(run.started_at)}
                    </span>
                  </td>
                  <td data-label="Status">
                    <Badge
                      tone={run.status === "COMPLETED" ? "success" : run.status === "FAILED" ? "danger" : "info"}
                      dot
                    >
                      {humanize(run.status)}
                    </Badge>
                  </td>
                  <td data-label="Cases">
                    {run.cases_scored} / {run.cases_total}
                  </td>
                  <td data-label="Answerer">{componentName(run.configuration, "answerer")}</td>
                  <td data-label="Reranker">{componentName(run.configuration, "reranker")}</td>
                  <td data-label="Took" className="nowrap">
                    {run.duration_ms ? formatDuration(run.duration_ms) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}

// --- The tab ------------------------------------------------------------------------

export function QualityTab({ report, loading }: { report: AnalyticsReport | null; loading: boolean }) {
  const dates = report?.questions.series.map((point) => point.date) ?? [];
  const abstentions = report?.abstentions ?? [];
  const stages = report?.latency.stages ?? [];
  return (
    <div className="stack">
      <div className="analytics-grid">
        <Card title="Answer rate" icon="check-circle" description="Share of questions answered from the documents, day by day.">
          <LineChart
            title="Answer rate per day"
            labels={dates}
            loading={loading}
            area
            yMax={1}
            format={ratio}
            axisFormat={(value) => formatPercent(value)}
            series={[{ key: "rate", label: "Answered from documents", values: report?.kpis.answer_rate.series ?? [] }]}
            empty="No questions were asked in this period."
          />
        </Card>
        <Card title="How answers were written" icon="sparkles" description="Written by the language model from the sources, or quoted from them verbatim.">
          <BarChart
            stacked
            title="Answers by how they were written"
            labels={dates}
            loading={loading}
            integer
            series={[
              { key: "generative", label: "Written from sources", values: report?.questions.series.map((p) => p.generative) ?? [] },
              { key: "quoted", label: "Quoted from sources", values: report?.questions.series.map((p) => p.quoted) ?? [] },
            ]}
            empty="No questions were answered from the documents in this period."
          />
        </Card>
        <Card title="Why questions went unanswered" icon="info" description="The system abstains rather than guess. These are its reasons.">
          <BarList
            title="Reasons questions went unanswered"
            loading={loading}
            items={abstentions.map((row) => ({
              key: row.code,
              label: ABSTENTION_LABEL[row.code] ?? humanize(row.code),
              title: explainAbstention(row.reason),
              hint: explainAbstention(row.reason),
              value: row.count,
            }))}
            empty="Every question in this period got an answer."
          />
        </Card>
        <Card title="Feedback" icon="thumbs-up" description="Ratings people gave answers, by day.">
          <BarChart
            stacked
            title="Feedback per day"
            labels={dates}
            loading={loading}
            integer
            series={[
              { key: "helpful", label: "Helpful", values: report?.feedback.series.map((p) => p.helpful) ?? [] },
              { key: "incorrect", label: "Incorrect", values: report?.feedback.series.map((p) => p.incorrect) ?? [] },
              { key: "not_helpful", label: "Not helpful", values: report?.feedback.series.map((p) => p.not_helpful) ?? [] },
            ]}
            empty="No answers were rated in this period."
          />
        </Card>
        <Card title="Where answer time goes" icon="clock" description="Median time of each step, over the answers that ran it.">
          <BarList
            title="Median time per answer step"
            loading={loading}
            format={formatDuration}
            color={2}
            items={stages.map((stage) => ({
              key: stage.stage,
              label: ANSWER_STAGE_LABEL[stage.stage] ?? humanize(stage.stage),
              hint: `${stage.answers.toLocaleString()} answers`,
              value: stage.median_ms ?? 0,
            }))}
            empty="No answers were timed in this period."
          />
        </Card>
      </div>
      <FeedbackQueue systemScope={report?.scope.department === null} />
      <Benchmarks />
    </div>
  );
}
