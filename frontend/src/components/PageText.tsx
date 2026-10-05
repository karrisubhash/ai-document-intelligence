import { useEffect, useRef } from "react";

import { layeredSegments, toUtf16Offset } from "../lib/highlight";

/**
 * One page of extracted text, with the cited passage and - inside it - the
 * exact quoted sentences highlighted.
 *
 * Whitespace is preserved because the page text is the evidence: tables
 * are laid out in columns, clauses are numbered on their own lines, and
 * reflowing them would present something the document does not say. The
 * strongest highlight scrolls into view so a citation lands the reader ON
 * the words.
 */
export default function PageText({
  text,
  start,
  end,
  quotes = [],
  label,
}: {
  text: string;
  start?: number | null;
  end?: number | null;
  quotes?: [number, number][];
  label?: string;
}) {
  const target = useRef<HTMLElement>(null);
  // `start` and `end` are API offsets (code points); `quotes` were found in
  // this string and are already JavaScript indices.
  const hasSpan =
    start !== null && start !== undefined && end !== null && end !== undefined;
  const span: [number, number] | null = hasSpan
    ? [toUtf16Offset(text, start), toUtf16Offset(text, end)]
    : null;
  // With quotes, the span is context and the quotes are the evidence. With
  // none - a search hit, an extracted field - the span IS the evidence and
  // gets the strong highlight itself.
  const segments = quotes.length
    ? layeredSegments(text, span, quotes)
    : layeredSegments(text, null, span ? [span] : []);
  const strongest = Math.max(...segments.map((segment) => segment.level));
  const key = `${start}-${end}-${quotes.map((quote) => quote.join(":")).join(",")}`;

  useEffect(() => {
    if (strongest > 0) target.current?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [strongest, key, text]);

  if (!text.trim()) {
    return <p className="muted">No text was extracted from this page.</p>;
  }

  let anchored = false;
  return (
    <div className="page-text" aria-label={label} tabIndex={0}>
      {segments.map((segment, index) => {
        if (segment.level === 0) return <span key={index}>{segment.text}</span>;
        const anchor = !anchored && segment.level === strongest;
        if (anchor) anchored = true;
        return (
          <mark
            key={index}
            ref={anchor ? target : undefined}
            className={segment.level === 2 ? "evidence" : "evidence-region"}
          >
            {segment.text}
          </mark>
        );
      })}
    </div>
  );
}
