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

const SURROGATE = /[\uD800-\uDFFF]/;

/**
 * An API offset, converted to an index into the same JavaScript string.
 *
 * Every offset the API returns (chunks, extracted fields, tables, search
 * hits) counts Unicode code points, because that is what a Python string
 * indexes. A JavaScript string indexes UTF-16 code units, so each character
 * outside the Basic Multilingual Plane before a span - an emoji, a
 * mathematical letter from a Word equation, a CJK Extension B character -
 * shifted the highlight one place to the left of the evidence.
 */
export function toUtf16Offset(text: string, codePoints: number): number {
  if (!SURROGATE.test(text)) return codePoints;
  let units = 0;
  let seen = 0;
  for (const character of text) {
    if (seen >= codePoints) break;
    units += character.length;
    seen += 1;
  }
  return units;
}

// A line that starts an item of its own: "(a)", "b)", "iv.", "2.", "3)", "4.1".
const ITEM_START = /^(?:\(?[a-z][.)]|\(?[ivxlc]{1,4}[.)]|\(?\d{1,3}[.)]|\d+(?:\.\d+)+)\s/i;
// Two or more spaces, or a tab, between words: a table's columns.
const COLUMN_GAP = /\S(?: {2,}|\t)\S/;

/**
 * PDF text is hard-wrapped at the page's line length, so a passage shown on
 * its own broke mid-sentence ("counted / separately for theory"). A line
 * that carries on in lower case, with a number or with "(" continues the
 * sentence and is joined to it. Every other break stays: after a sentence
 * (or a colon or semicolon), at a blank line, before a heading (a capital),
 * before an item of a list or a clause ("(a)", "ii)", "2.", "4.1"), and
 * around a table's row (columns two spaces apart). A word broken with a
 * hyphen at the line's end is joined without a space.
 */
export function unwrapLines(text: string): string {
  const out: string[] = [];
  let previous = "";
  for (const line of text.split(/\r?\n/)) {
    const next = line.trimStart();
    const head = out.length ? out[out.length - 1].trimEnd() : "";
    const joins =
      // The line before stopped mid-sentence: it is not blank and does not
      // end with a full stop, colon, semicolon or other sentence end ...
      /[^.:;!?]$/.test(head) &&
      // ... and this one carries the sentence on.
      /^[a-z0-9(]/.test(next) &&
      !ITEM_START.test(next) &&
      !COLUMN_GAP.test(previous.trim()) &&
      !COLUMN_GAP.test(next);
    if (joins) out[out.length - 1] = head + (head.endsWith("-") ? "" : " ") + next;
    else out.push(line);
    previous = line;
  }
  return out.join("\n");
}

interface Cell {
  character: string;
  on: boolean;
}

/**
 * unwrapLines, keeping track of which characters were highlighted: the
 * search highlights the server computes are offsets into the passage as
 * stored, and unwrapping removes and adds characters. `spans` are UTF-16
 * offsets into `text`; a space put in where two lines were joined is
 * highlighted when the words on both sides are.
 */
export function unwrapSegments(text: string, spans: [number, number][]): Segment[] {
  const marked = new Uint8Array(text.length);
  for (const [start, end] of spans) {
    for (let index = Math.max(0, start); index < Math.min(end, text.length); index += 1) marked[index] = 1;
  }

  const lines: Cell[][] = [];
  let previous = "";
  let lineStart = 0;
  for (let index = 0; index <= text.length; index += 1) {
    if (index < text.length && text[index] !== "\n") continue;
    let lineEnd = index;
    if (lineEnd > lineStart && text[lineEnd - 1] === "\r") lineEnd -= 1;
    const line = text.slice(lineStart, lineEnd);
    const lead = line.length - line.trimStart().length;
    const next = line.slice(lead);
    const last = lines[lines.length - 1];
    const head = last ? last.map((cell) => cell.character).join("").trimEnd() : "";
    const joins =
      // The line before stopped mid-sentence: it is not blank and does not
      // end with a full stop, colon, semicolon or other sentence end ...
      /[^.:;!?]$/.test(head) &&
      // ... and this one carries the sentence on.
      /^[a-z0-9(]/.test(next) &&
      !ITEM_START.test(next) &&
      !COLUMN_GAP.test(previous.trim()) &&
      !COLUMN_GAP.test(next);
    if (joins) {
      while (last.length && /\s/.test(last[last.length - 1].character)) last.pop();
      const before = last[last.length - 1];
      const firstOn = Boolean(marked[lineStart + lead]);
      if (!head.endsWith("-")) last.push({ character: " ", on: Boolean(before?.on && firstOn) });
      for (let offset = 0; offset < next.length; offset += 1) {
        last.push({ character: next[offset], on: Boolean(marked[lineStart + lead + offset]) });
      }
    } else {
      lines.push(
        Array.from(line, (_, offset) => ({ character: line[offset], on: Boolean(marked[lineStart + offset]) })),
      );
    }
    previous = line;
    lineStart = index + 1;
  }

  const segments: Segment[] = [];
  lines.forEach((cells, number) => {
    const all = number ? [{ character: "\n", on: false }, ...cells] : cells;
    for (const cell of all) {
      const tail = segments[segments.length - 1];
      if (tail && tail.highlighted === cell.on) tail.text += cell.character;
      else segments.push({ text: cell.character, highlighted: cell.on });
    }
  });
  return segments;
}

/**
 * A search passage ready to show: the server's snippet window of `text`,
 * unwrapped, with the server's highlights. Offsets are code points (API);
 * `clippedStart`/`clippedEnd` say whether text was cut on either side.
 */
export function passageSegments(
  text: string,
  highlights: { start: number; end: number }[],
  snippet?: { start: number; end: number },
): { segments: Segment[]; clippedStart: boolean; clippedEnd: boolean } {
  const from = snippet ? toUtf16Offset(text, snippet.start) : 0;
  const to = snippet ? toUtf16Offset(text, snippet.end) : text.length;
  const window = text.slice(from, to);
  const spans = highlights
    .map(({ start, end }) => [toUtf16Offset(text, start) - from, toUtf16Offset(text, end) - from] as [number, number])
    .filter(([start, end]) => end > 0 && start < window.length);
  return {
    segments: unwrapSegments(window, spans),
    clippedStart: from > 0,
    clippedEnd: to < text.length,
  };
}
