export interface Segment {
  text: string;
  highlighted: boolean;
}

/**
 * Splits text around one character span, clamping a span that falls
 * outside the text rather than throwing - a citation from an older pass
 * can be longer than today's page, and a crash is worse than a partial
 * highlight.
 */
export function splitAtSpan(text: string, start: number, end: number): Segment[] {
  const from = Math.max(0, Math.min(start, text.length));
  const to = Math.max(from, Math.min(end, text.length));
  const segments: Segment[] = [];
  if (from > 0) segments.push({ text: text.slice(0, from), highlighted: false });
  if (to > from) segments.push({ text: text.slice(from, to), highlighted: true });
  if (to < text.length) segments.push({ text: text.slice(to), highlighted: false });
  return segments.length ? segments : [{ text, highlighted: false }];
}

const STOPWORDS = new Set(
  "a an and are as at be by can do does for from how i in is it of on or the to was what when where which who why will with".split(
    " ",
  ),
);

export function queryTerms(query: string): string[] {
  const terms = query
    .toLowerCase()
    .split(/[^\p{L}\p{N}./-]+/u)
    .map((term) => term.replace(/^[./-]+|[./-]+$/g, ""))
    .filter((term) => term.length > 1 && !STOPWORDS.has(term));
  return [...new Set(terms)].sort((a, b) => b.length - a.length);
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Marks every occurrence of the query's content terms, case-insensitively.
 * Prefix matching on purpose - "attend" should light up "attendance" - so a
 * reader can see WHY a passage came back, which is most of how they decide
 * whether to trust it.
 */
export function highlightTerms(text: string, query: string): Segment[] {
  const terms = queryTerms(query);
  if (!terms.length || !text) return [{ text, highlighted: false }];

  const pattern = new RegExp(`(${terms.map(escapeRegExp).join("|")})`, "giu");
  const segments: Segment[] = [];
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    const index = match.index ?? 0;
    if (index > last) segments.push({ text: text.slice(last, index), highlighted: false });
    segments.push({ text: match[0], highlighted: true });
    last = index + match[0].length;
  }
  if (last < text.length) segments.push({ text: text.slice(last), highlighted: false });
  return segments;
}

/** A window of text around a span, for compact previews. */
export function excerpt(text: string, start: number, end: number, context = 160) {
  const from = Math.max(0, start - context);
  const to = Math.min(text.length, end + context);
  return {
    text: text.slice(from, to),
    start: start - from,
    end: end - from,
    clippedStart: from > 0,
    clippedEnd: to < text.length,
  };
}

export interface LayeredSegment {
  text: string;
  /** 0 = plain, 1 = inside the cited passage, 2 = the words actually quoted. */
  level: 0 | 1 | 2;
}

/**
 * Text split into layers: the cited passage, and inside it the exact
 * sentences an answer quoted. A cited chunk can be a whole page, and a
 * highlight covering a whole page tells the reader nothing about WHICH
 * words carried the answer.
 */
export function layeredSegments(
  text: string,
  passage: [number, number] | null,
  quotes: [number, number][],
): LayeredSegment[] {
  const levels = new Uint8Array(text.length);
  const paint = ([start, end]: [number, number], level: number) => {
    const from = Math.max(0, Math.min(start, text.length));
    const to = Math.max(from, Math.min(end, text.length));
    for (let index = from; index < to; index += 1) {
      if (levels[index] < level) levels[index] = level;
    }
  };
  if (passage) paint(passage, 1);
  for (const quote of quotes) paint(quote, 2);

  const segments: LayeredSegment[] = [];
  let start = 0;
  for (let index = 1; index <= text.length; index += 1) {
    if (index === text.length || levels[index] !== levels[start]) {
      segments.push({ text: text.slice(start, index), level: levels[start] as 0 | 1 | 2 });
      start = index;
    }
  }
  return segments.length ? segments : [{ text, level: 0 }];
}

/**
 * Where an answer's sentences appear, verbatim, in a page.
 *
 * Whitespace-insensitive, because a quote joins lines the page wraps
 * ("...in each course to be eligible"), and bounded to the given region so
 * a sentence repeated elsewhere on the page is not highlighted there.
 * Generative answers paraphrase and will usually match nothing - which is
 * the honest outcome: only the passage is then highlighted.
 */
export function findQuotedSpans(
  answer: string,
  text: string,
  region: [number, number] = [0, text.length],
): [number, number][] {
  const sentences = answer
    .replace(/\[\d{1,3}\]/g, "\n")
    .split(/(?<=[.!?])\s+|\n+/)
    .map((sentence) => sentence.trim())
    .filter((sentence) => sentence.length >= 20);

  const spans: [number, number][] = [];
  const within = text.slice(region[0], region[1]);
  for (const sentence of sentences) {
    const words = sentence.split(/\s+/).map(escapeRegExp);
    const match = new RegExp(words.join("\\s+")).exec(within);
    if (match) spans.push([region[0] + match.index, region[0] + match.index + match[0].length]);
  }
  return spans;
}
