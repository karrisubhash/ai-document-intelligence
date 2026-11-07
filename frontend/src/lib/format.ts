const dateTime = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});
const dateOnly = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" });
const relative = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : dateTime.format(date);
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : dateOnly.format(date);
}

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 365 * 24 * 3600],
  ["month", 30 * 24 * 3600],
  ["week", 7 * 24 * 3600],
  ["day", 24 * 3600],
  ["hour", 3600],
  ["minute", 60],
];

/** "3 minutes ago". Falls back to seconds, never to a blank. */
export function formatRelative(value: string | null | undefined, now = Date.now()): string {
  if (!value) return "—";
  const then = new Date(value).getTime();
  if (Number.isNaN(then)) return "—";
  const seconds = Math.round((then - now) / 1000);
  for (const [unit, size] of UNITS) {
    if (Math.abs(seconds) >= size) return relative.format(Math.round(seconds / size), unit);
  }
  return relative.format(seconds, "second");
}

// A number and its unit are joined by a no-break space: a wrapped line
// never leaves "MB" or "s" on its own.
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || Number.isNaN(bytes)) return "—";
  if (bytes < 1024) return `${bytes}\u00a0B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value < 10 ? 1 : 0)}\u00a0${units[unit]}`;
}

export function formatDuration(milliseconds: number | null | undefined): string {
  if (milliseconds === null || milliseconds === undefined) return "—";
  if (milliseconds < 1000) return `${Math.round(milliseconds)}\u00a0ms`;
  return `${(milliseconds / 1000).toFixed(milliseconds < 10_000 ? 1 : 0)}\u00a0s`;
}

export function formatPercent(fraction: number | null | undefined, digits = 0): string {
  if (fraction === null || fraction === undefined || Number.isNaN(fraction)) return "—";
  return `${(fraction * 100).toFixed(digits)}%`;
}

const integer = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
const compact = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });

/** 1,234 - for counts, axis values and tooltips. */
export function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return Number.isInteger(value) ? integer.format(value) : value.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

/** 1.2K - for tight places such as an axis tick. */
export function formatCompact(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return Math.abs(value) < 1000 ? formatNumber(value) : compact.format(value);
}

/**
 * A calendar day sent as "YYYY-MM-DD", as a LOCAL date. `new Date("2026-11-07")`
 * is midnight UTC, which west of Greenwich is the evening of the 6th - so a
 * day bucket would be labelled with the day before.
 */
export function parseLocalDate(value: string): Date | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return null;
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  return Number.isNaN(date.getTime()) ? null : date;
}

const shortDay = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });
const longDay = new Intl.DateTimeFormat(undefined, {
  weekday: "short",
  month: "short",
  day: "numeric",
  year: "numeric",
});

/** "Nov 7" for a "YYYY-MM-DD" day; the input itself if it is not one. */
export function formatShortDay(value: string): string {
  const date = parseLocalDate(value);
  return date ? shortDay.format(date) : value;
}

/** "Sat, Nov 7, 2026" for a "YYYY-MM-DD" day; the input itself if it is not one. */
export function formatLongDay(value: string): string {
  const date = parseLocalDate(value);
  return date ? longDay.format(date) : value;
}

export function pluralize(count: number, singular: string, plural = `${singular}s`) {
  return `${count.toLocaleString()} ${count === 1 ? singular : plural}`;
}

export function displayName(user: {
  first_name?: string;
  last_name?: string;
  username: string;
}): string {
  const full = [user.first_name, user.last_name].filter(Boolean).join(" ").trim();
  return full || user.username;
}
