/**
 * A deliberately small Markdown renderer for answers.
 *
 * Generative answerers write short Markdown - a paragraph, a list, some bold
 * - and the answer text is ultimately derived from uploaded documents, which
 * are untrusted. So this never produces HTML from a string: it builds React
 * elements from text, and React escapes every character of it. There is no
 * `dangerouslySetInnerHTML` path, no link rendering and no image rendering,
 * so a document that says `<img src=x onerror=...>` or `[click](javascript:)`
 * renders as those literal characters.
 *
 * Citation markers like [3] become buttons through `renderCitation`.
 */

import { Fragment, type ReactNode } from "react";

type CitationRenderer = (marker: number, key: string) => ReactNode;

// Bold may contain italics ("**only *on* medical grounds**"). Italics need
// a non-space just inside each `*` and no word character just outside, so
// arithmetic such as "5 * 3" or "2*3*4" stays literal. Underscore italics
// are deliberately not supported: identifiers like file_name_v2 would break.
const INLINE =
  /(\*\*(?:[^*]|\*(?!\*))+\*\*|(?<![\w*])\*(?![\s*])[^*\n]+?(?<!\s)\*(?![\w*])|`[^`]+`|\[\d{1,3}\])/g;

function renderInline(text: string, renderCitation: CitationRenderer | undefined, keyBase: string) {
  const parts: ReactNode[] = [];
  let index = 0;
  for (const piece of text.split(INLINE)) {
    if (!piece) continue;
    const key = `${keyBase}-${index++}`;
    if (piece.startsWith("**") && piece.endsWith("**") && piece.length > 4) {
      // Rendered inline in turn, so a marker inside bold is still a button.
      // Each level strips a pair of `*`, so the recursion terminates.
      parts.push(<strong key={key}>{renderInline(piece.slice(2, -2), renderCitation, key)}</strong>);
    } else if (piece.startsWith("*") && piece.endsWith("*") && piece.length > 2) {
      parts.push(<em key={key}>{renderInline(piece.slice(1, -1), renderCitation, key)}</em>);
    } else if (piece.startsWith("`") && piece.endsWith("`") && piece.length > 2) {
      parts.push(<code key={key}>{piece.slice(1, -1)}</code>);
    } else if (/^\[\d{1,3}\]$/.test(piece) && renderCitation) {
      parts.push(renderCitation(Number(piece.slice(1, -1)), key));
    } else {
      parts.push(<Fragment key={key}>{piece}</Fragment>);
    }
  }
  return parts;
}

type Block =
  | { kind: "paragraph"; lines: string[] }
  | { kind: "list"; ordered: boolean; items: string[]; numbers: number[] }
  | { kind: "heading"; text: string };

const BULLET = /^\s*[-*•]\s+(.*)$/;
const NUMBERED = /^\s*(\d{1,2})[.)]\s+(.*)$/;
// A line whose columns are separated by a tab or a run of spaces: a table
// row, as the extractive answerer quotes one.
const TABULAR = /\t| {2,}/;
const HEADING = /^\s*#{1,6}\s+(.*)$/;
// "---", "***", "___" or "* * *": a generative answer's section break. Shown
// as text it read as stray punctuation; it ends the current block instead.
const RULE = /^\s*([-*_])(?:\s*\1){2,}\s*$/;

export function parseBlocks(source: string): Block[] {
  const blocks: Block[] = [];
  let current: Block | null = null;

  const flush = () => {
    if (current) blocks.push(current);
    current = null;
  };

  for (const raw of source.replace(/\r\n/g, "\n").split("\n")) {
    const line = raw.trimEnd();
    if (!line.trim()) {
      flush();
      continue;
    }

    if (RULE.test(line)) {
      flush();
      continue;
    }

    const heading = HEADING.exec(line);
    const bullet = BULLET.exec(line);
    const numbered = NUMBERED.exec(line);

    if (heading) {
      flush();
      blocks.push({ kind: "heading", text: heading[1] });
    } else if (bullet || numbered) {
      const ordered = Boolean(numbered && !bullet);
      const item = bullet ? bullet[1] : numbered![2];
      // The source's own number, kept: a quoted "12) ..." clause rendered as
      // "1." is a different clause.
      const number = ordered ? Number(numbered![1]) : 0;
      const open = current as Block | null;
      if (open?.kind === "list" && open.ordered === ordered) {
        open.items.push(item);
        open.numbers.push(number);
      } else {
        flush();
        current = { kind: "list", ordered, items: [item], numbers: [number] };
      }
    } else {
      const open = current as Block | null;
      if (open?.kind === "paragraph") {
        open.lines.push(line.trim());
      } else if (open?.kind === "list") {
        // A wrapped list item continues the last item.
        open.items[open.items.length - 1] += ` ${line.trim()}`;
      } else {
        flush();
        current = { kind: "paragraph", lines: [line.trim()] };
      }
    }
  }
  flush();
  return blocks;
}

export function Markdown({
  source,
  renderCitation,
}: {
  source: string;
  renderCitation?: CitationRenderer;
}) {
  const blocks = parseBlocks(source);
  return (
    <div className="prose">
      {blocks.map((block, index) => {
        const key = `b${index}`;
        if (block.kind === "heading") {
          return (
            <p key={key} className="prose-heading">
              {renderInline(block.text, renderCitation, key)}
            </p>
          );
        }
        if (block.kind === "list") {
          const Tag = block.ordered ? "ol" : "ul";
          return (
            <Tag key={key}>
              {block.items.map((item, itemIndex) => (
                <li
                  key={`${key}-${itemIndex}`}
                  value={block.ordered ? block.numbers[itemIndex] : undefined}
                >
                  {renderInline(item, renderCitation, `${key}-${itemIndex}`)}
                </li>
              ))}
            </Tag>
          );
        }
        if (block.lines.length > 1 && block.lines.some((line) => TABULAR.test(line))) {
          // A quoted table: rows on their own lines and columns apart. Joined
          // into one paragraph, every row and column boundary disappeared -
          // and the extractive answerer quotes a whole table precisely so
          // the value can be read off it.
          return (
            <div key={key} className="prose-table">
              {block.lines.map((line, lineIndex) => (
                <div key={`${key}-${lineIndex}`} className="prose-table-row">
                  {renderInline(line, renderCitation, `${key}-${lineIndex}`)}
                </div>
              ))}
            </div>
          );
        }
        return <p key={key}>{renderInline(block.lines.join(" "), renderCitation, key)}</p>;
      })}
    </div>
  );
}
