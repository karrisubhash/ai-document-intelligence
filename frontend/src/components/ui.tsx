import { useState, type ReactNode } from "react";

/** Coloured status pill. The tone carries the meaning, not just the text. */
export type Tone = "ok" | "warn" | "bad" | "busy" | "muted" | "info";

export function Pill({
  tone = "muted",
  children,
  title,
}: {
  tone?: Tone;
  children: ReactNode;
  title?: string;
}) {
  return (
    <span className={`pill pill-${tone}`} title={title}>
      {children}
    </span>
  );
}

export function toneForDocumentStatus(status: string): Tone {
  switch (status) {
    case "COMPLETED":
      return "ok";
    case "FAILED":
      return "bad";
    case "PROCESSING":
    case "QUEUED":
      return "busy";
    default:
      return "muted";
  }
}

export function toneForEventStatus(status: string): Tone {
  switch (status) {
    case "SUCCEEDED":
      return "ok";
    case "FAILED":
      return "bad";
    case "SKIPPED":
      return "warn";
    case "STARTED":
      return "busy";
    default:
      return "muted";
  }
}

export function toneForFieldStatus(status: string): Tone {
  switch (status) {
    case "FOUND":
      return "ok";
    case "INVALID":
      return "warn";
    case "CONFLICTED":
      return "bad";
    default:
      return "muted";
  }
}

export function Banner({
  tone = "info",
  title,
  children,
}: {
  tone?: Tone;
  title?: string;
  children?: ReactNode;
}) {
  return (
    <div className={`banner banner-${tone}`}>
      {title ? <strong>{title}</strong> : null}
      {children ? <div className="banner-body">{children}</div> : null}
    </div>
  );
}

/**
 * What is shown when there is nothing to show. It always says why, because
 * "no classification verdict" and "classification was skipped" are
 * different facts and a blank panel conflates them.
 */
export function Empty({ children }: { children: ReactNode }) {
  return <p className="empty">{children}</p>;
}

export function ErrorBox({ error }: { error: unknown }) {
  if (!error) return null;
  const message = error instanceof Error ? error.message : String(error);
  return <div className="banner banner-bad">{message}</div>;
}

export function Spinner({ label = "Loading…" }: { label?: string }) {
  return <p className="empty">{label}</p>;
}

/** Collapsed raw JSON, for the metadata every stage and result carries. */
export function JsonBlock({
  value,
  label = "metadata",
}: {
  value: unknown;
  label?: string;
}) {
  const isEmpty =
    value === null ||
    value === undefined ||
    (typeof value === "object" && Object.keys(value as object).length === 0);

  if (isEmpty) return null;

  return (
    <details className="json">
      <summary>{label}</summary>
      <pre>{JSON.stringify(value, null, 2)}</pre>
    </details>
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="field">
      <span className="field-label">{label}</span>
      <span className="field-value">{children}</span>
    </div>
  );
}

export function Tabs<T extends string>({
  tabs,
  active,
  onChange,
}: {
  tabs: readonly { id: T; label: string }[];
  active: T;
  onChange: (id: T) => void;
}) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((tab) => (
        <button
          key={tab.id}
          role="tab"
          aria-selected={tab.id === active}
          className={tab.id === active ? "tab tab-active" : "tab"}
          onClick={() => onChange(tab.id)}
        >
          {tab.label}
        </button>
      ))}
    </div>
  );
}

/** Long verbatim text - page text, chunk text - behind a show-more. */
export function Excerpt({ text, limit = 600 }: { text: string; limit?: number }) {
  const [expanded, setExpanded] = useState(false);

  if (!text) return <Empty>No text.</Empty>;
  if (text.length <= limit) return <pre className="excerpt">{text}</pre>;

  return (
    <div>
      <pre className="excerpt">{expanded ? text : `${text.slice(0, limit)}…`}</pre>
      <button className="link" onClick={() => setExpanded(!expanded)}>
        {expanded ? "Show less" : `Show all ${text.length.toLocaleString()} characters`}
      </button>
    </div>
  );
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

export function formatTime(value: string | null): string {
  if (!value) return "—";
  return new Date(value).toLocaleString();
}

/** Wall time of a stage, from its own timestamps rather than guessed. */
export function duration(start: string | null, end: string | null): string {
  if (!start || !end) return "—";
  const ms = new Date(end).getTime() - new Date(start).getTime();
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(2)} s`;
}
