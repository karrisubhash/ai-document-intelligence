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

const INLINE = /(\*\*[^*]+\*\*|`[^`]+`|\[\d{1,3}\])/g;

function renderInline(text: string, renderCitation: CitationRenderer | undefined, keyBase: string) {
  const parts: ReactNode[] = [];
  let index = 0;
  for (const piece of text.split(INLINE)) {
    if (!piece) continue;
    const key = `${keyBase}-${index++}`;
    if (piece.startsWith("**") && piece.endsWith("**") && piece.length > 4) {
      parts.push(<strong key={key}>{piece.slice(2, -2)}</strong>);
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
  | { kind: "list"; ordered: boolean; items: string[] }
  | { kind: "heading"; text: string };

const BULLET = /^\s*[-*•]\s+(.*)$/;
const NUMBERED = /^\s*\d{1,2}[.)]\s+(.*)$/;
const HEADING = /^\s*#{1,6}\s+(.*)$/;

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

    const heading = HEADING.exec(line);
    const bullet = BULLET.exec(line);
    const numbered = NUMBERED.exec(line);

    if (heading) {
      flush();
      blocks.push({ kind: "heading", text: heading[1] });
    } else if (bullet || numbered) {
      const ordered = Boolean(numbered && !bullet);
      const item = (bullet ?? numbered)![1];
      const open = current as Block | null;
      if (open?.kind === "list" && open.ordered === ordered) {
        open.items.push(item);
      } else {
        flush();
        current = { kind: "list", ordered, items: [item] };
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
                <li key={`${key}-${itemIndex}`}>
                  {renderInline(item, renderCitation, `${key}-${itemIndex}`)}
                </li>
              ))}
            </Tag>
          );
        }
        return <p key={key}>{renderInline(block.lines.join(" "), renderCitation, key)}</p>;
      })}
    </div>
  );
}
