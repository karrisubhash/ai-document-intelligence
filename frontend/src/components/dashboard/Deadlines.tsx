import { Link } from "react-router-dom";

import { passageLink, type Deadline } from "../../api/home";
import { formatShortDay, parseLocalDate } from "../../lib/format";
import { Icon } from "../icons";
import { Card } from "../ui";

const relative = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
const month = new Intl.DateTimeFormat(undefined, { month: "short" });

/** "today", "tomorrow", "in 5 days" for a "YYYY-MM-DD" day. */
export function daysFromNow(day: string, now = new Date()): string {
  const date = parseLocalDate(day);
  if (!date) return "";
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  return relative.format(Math.round((date.getTime() - today.getTime()) / 86_400_000), "day");
}

/**
 * Dates found in the reader's documents - closing dates, fee deadlines,
 * exam days - soonest first, each opening the page it was read from with
 * the sentence highlighted. Extracted, never inferred: the sentence is
 * shown so the reader can judge it.
 */
export function Deadlines({ deadlines, limit = 6 }: { deadlines: Deadline[]; limit?: number }) {
  const shown = [...deadlines].sort((a, b) => a.date.localeCompare(b.date)).slice(0, limit);
  return (
    <Card
      title="Upcoming dates"
      icon="calendar"
      description="Found in your documents for the next 90 days. Check the source before you rely on one."
      padded={false}
    >
      <ul className="deadline-list">
        {shown.map((deadline) => {
          const date = parseLocalDate(deadline.date);
          return (
            <li key={`${deadline.document_id}-${deadline.date}-${deadline.start}`} className="deadline">
              <span className="deadline-date" aria-hidden="true">
                <span className="deadline-month">{date ? month.format(date) : ""}</span>
                <span className="deadline-day">{date ? date.getDate() : "?"}</span>
              </span>
              <span className="deadline-text">
                <Link to={passageLink(deadline)} className="deadline-link">
                  {deadline.label || deadline.text}
                  <span className="visually-hidden">, {formatShortDay(deadline.date)}</span>
                </Link>
                {deadline.label && deadline.text ? <span className="deadline-quote">“{deadline.text}”</span> : null}
                <span className="deadline-meta">
                  <Icon name="file-text" size={12} />
                  {deadline.document_title} · p. {deadline.page_number} · {daysFromNow(deadline.date)}
                </span>
              </span>
            </li>
          );
        })}
      </ul>
    </Card>
  );
}
