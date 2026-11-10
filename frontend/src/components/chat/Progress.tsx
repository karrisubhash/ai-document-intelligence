import type { StreamSource } from "../../api/types";
import { Icon } from "../icons";

/** The steps a streamed answer goes through, as the server reports them. */
export const STAGES: [string, string][] = [
  ["retrieving", "Searching your documents"],
  ["ranking", "Ranking passages"],
  ["writing", "Writing the answer"],
  ["verifying", "Checking every figure"],
];

export function stageLabel(stage: string | undefined): string {
  return STAGES.find(([key]) => key === stage)?.[1] ?? STAGES[0][1];
}

/** One chip per document page, in the order the passages were found. */
function chips(sources: StreamSource[]) {
  const seen = new Set<string>();
  return sources.filter((source) => {
    const key = `${source.document_id}:${source.page_number ?? ""}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

/**
 * Where a streamed answer has got to, and what it is reading: replaces a
 * generic "thinking" line, so a long rerank or a slow model reads as work in
 * progress rather than a hang. The sources are provisional - the answer's own
 * cited sources replace them when it arrives.
 */
export function AnswerProgress({
  stage,
  sources = [],
  compact = false,
}: {
  stage?: string;
  sources?: StreamSource[];
  /** While the answer's words are already showing: the sources only. */
  compact?: boolean;
}) {
  const current = Math.max(
    0,
    STAGES.findIndex(([key]) => key === stage),
  );
  const shown = chips(sources);

  return (
    <div className="answer-progress">
      {compact ? null : (
        <ol className="stepper" aria-label="Progress">
          {STAGES.map(([key, label], index) => (
            <li
              key={key}
              className={`step${index < current ? " step-done" : index === current ? " step-current" : ""}`}
              aria-current={index === current ? "step" : undefined}
            >
              <span className="step-dot" aria-hidden="true">
                {index < current ? <Icon name="check" size={11} strokeWidth={3} /> : null}
              </span>
              <span className="step-label">{label}</span>
            </li>
          ))}
        </ol>
      )}
      {shown.length ? (
        <div className="reading" aria-label="Reading">
          <span className="reading-label">Reading</span>
          <ul className="reading-list">
            {shown.slice(0, 6).map((source) => (
              <li key={`${source.document_id}:${source.page_number}`} className="reading-chip">
                <Icon name="file-text" size={12} />
                <span className="reading-title">{source.document_title}</span>
                {source.page_number ? <span className="reading-page">p. {source.page_number}</span> : null}
              </li>
            ))}
            {shown.length > 6 ? <li className="reading-more">+{shown.length - 6} more</li> : null}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
