import { useEffect, useState } from "react";

import { aiApi } from "../../api/ai";
import { usePolling } from "../../api/hooks";
import type { AILink, AILinkState, AIProbeResult, AIStatus } from "../../api/types";
import { Icon } from "../../components/icons";
import { Badge, Button, Card, Meter, errorMessage } from "../../components/ui";
import { formatDuration, formatRelative } from "../../lib/format";
import type { Tone } from "../../lib/labels";
import "../../styles/ai.css";

const STATE: Record<AILinkState, { label: string; tone: Tone }> = {
  ready: { label: "Ready", tone: "success" },
  cooling: { label: "Cooling down", tone: "warning" },
  exhausted_today: { label: "Used up today", tone: "danger" },
  unconfigured: { label: "No API key", tone: "muted" },
};

/** Refreshed this often while the page is open and visible. */
const POLL_MS = 30_000;

/** "14:05 GMT+5:30": the viewer's clock, with its zone, because quota days end in other zones. */
export function clockTime(value: string | null): string {
  if (!value) return "";
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    timeZoneName: "short",
  }).format(new Date(value));
}

export function summary(status: AIStatus): string {
  if (!status.enabled || !status.chain.length) {
    return "No generative model is configured, so answers quote your documents directly.";
  }
  const { totals } = status;
  if (!totals.generative_available) {
    return totals.resumes_at
      ? `AI writing is paused; answers quote your documents until about ${clockTime(totals.resumes_at)}.`
      : "No model can write answers right now; answers quote your documents directly.";
  }
  if (totals.capacity_left_today === null) {
    return "At least one model has no daily limit.";
  }
  const left = totals.capacity_left_today;
  const reset = totals.resets_at ? ` · resets at ${clockTime(totals.resets_at)}` : "";
  return `≈${left} generative ${left === 1 ? "answer" : "answers"} left today${reset}`;
}

function stateDetail(link: AILink): string {
  switch (link.state) {
    case "cooling":
      return `Rate-limited; tried again after ${clockTime(link.cooling_until)}`;
    case "exhausted_today":
      return `Today's allowance is spent; resets at ${clockTime(link.resets_at)}`;
    case "unconfigured":
      return `Set ${link.key_env} on the server to use this model`;
    default:
      return link.last_success_at
        ? `Last answered ${formatRelative(link.last_success_at)}`
        : "Not used yet today";
  }
}

function probeText(result: AIProbeResult): string {
  if (result.ok) return `Answered in ${formatDuration(result.latency_ms)}`;
  if (result.error_type === "unconfigured") return "Not sent: no API key is set";
  const status = result.status_code ? ` (${result.status_code})` : "";
  return `Failed: ${result.error_type ?? "error"}${status}`;
}

function LinkRow({
  link,
  onTested,
}: {
  link: AILink;
  onTested: (result: AIProbeResult) => void;
}) {
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const state = STATE[link.state] ?? STATE.ready;

  const test = async () => {
    setTesting(true);
    setResult(null);
    try {
      const probe = await aiApi.test(link.id);
      setResult({ ok: probe.ok, text: probeText(probe) });
      onTested(probe);
    } catch (caught) {
      setResult({ ok: false, text: errorMessage(caught) });
    } finally {
      setTesting(false);
    }
  };

  return (
    <li className={`ai-link ai-link-${link.state}`}>
      <span className="ai-dot" aria-hidden="true" />
      <div className="ai-link-main">
        <div className="ai-link-head">
          <span className="ai-link-name">{link.label}</span>
          <Badge tone={state.tone}>{state.label}</Badge>
          {link.trains_on_input ? (
            <Badge
              tone="warning"
              icon="shield"
              title="This provider's free tier may use what it is sent to improve its products."
            >
              May train on inputs
            </Badge>
          ) : null}
        </div>
        <code className="ai-link-id">{link.id}</code>
        <p className="ai-link-detail">{stateDetail(link)}</p>
        {link.last_error_type ? (
          <p className="ai-link-error">
            <Icon name="alert-circle" size={13} />
            Last error {link.last_error_type}
            {link.last_error_status ? ` (${link.last_error_status})` : ""}
            {link.last_error_at ? ` · ${formatRelative(link.last_error_at)}` : ""}
          </p>
        ) : null}
      </div>
      <div className="ai-link-usage">
        {link.daily_limit ? (
          <>
            <Meter
              value={link.calls_today / link.daily_limit}
              label={`${link.label}: ${link.calls_today} of ${link.daily_limit} requests used today`}
            />
            <span className="ai-link-count">
              {link.calls_today} / {link.daily_limit} today
            </span>
          </>
        ) : (
          <span className="ai-link-count">{link.calls_today} today · no daily limit</span>
        )}
      </div>
      <div className="ai-link-test">
        <Button size="sm" icon="zap" onClick={test} loading={testing} aria-label={`Test ${link.label}`}>
          Test
        </Button>
        {result ? (
          <span role="status" className={`ai-probe ${result.ok ? "ai-probe-ok" : "ai-probe-failed"}`}>
            {result.text}
          </span>
        ) : null}
      </div>
    </li>
  );
}

/**
 * System status → AI providers: one row per provider:model link of the
 * chain, today's usage against each daily limit, and a probe per link.
 * Starts from the System status payload and then polls the cheaper
 * /admin/ai/ endpoint while the page is visible.
 */
export function AIProvidersCard({ initial }: { initial: AIStatus | null }) {
  const [status, setStatus] = useState<AIStatus | null>(initial);
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => setStatus(initial), [initial]);

  const refresh = async () => {
    setRefreshing(true);
    try {
      setStatus(await aiApi.status());
    } catch {
      // Keep the last good state; System status itself reports failures.
    } finally {
      setRefreshing(false);
    }
  };

  usePolling(refresh, Boolean(status?.enabled), POLL_MS);

  if (!status) return null;

  const replaceRow = (result: AIProbeResult) =>
    setStatus((current) =>
      current
        ? {
            ...current,
            chain: current.chain.map((link) => (link.id === result.state.id ? result.state : link)),
          }
        : current,
    );

  const answers = status.totals.answers_today;

  return (
    <Card
      title="AI providers"
      icon="sparkles"
      className="ai-card"
      description={summary(status)}
      actions={
        status.enabled ? (
          <Button size="sm" icon="refresh" onClick={refresh} loading={refreshing}>
            Refresh
          </Button>
        ) : undefined
      }
    >
      {answers ? (
        <p className="ai-today">
          <Icon name="gauge" size={14} />
          Today: {answers.generative} written · {answers.quoted} quoted · {answers.abstained} not answered
          {answers.quoted_ai_unavailable
            ? ` · ${answers.quoted_ai_unavailable} quoted because no model could answer`
            : ""}
        </p>
      ) : null}
      {status.chain.length ? (
        <ul className="ai-links" aria-label="Model chain, in the order models are tried">
          {status.chain.map((link) => (
            <LinkRow key={link.id} link={link} onTested={replaceRow} />
          ))}
        </ul>
      ) : (
        <p className="ai-empty">
          Set RAG_ANSWERER and a provider's API key to have answers written by a model. See
          docs/ai/llm-providers.md.
        </p>
      )}
      {status.chain.length ? (
        <p className="ai-footnote">
          Tried top to bottom. Chat keeps the last {status.totals.chat_reserve} calls of the day;
          document briefs and other background AI work stop before them.
        </p>
      ) : null}
    </Card>
  );
}
