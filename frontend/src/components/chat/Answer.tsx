import { useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../../api/client";
import { useResource } from "../../api/hooks";
import type {
  ChatMessage,
  ChatResponse,
  Citation,
  FeedbackRating,
} from "../../api/types";
import { formatDuration, formatPercent } from "../../lib/format";
import { excerpt, findQuotedSpans, toUtf16Offset } from "../../lib/highlight";
import { explainAbstention } from "../../lib/labels";
import { Markdown } from "../../lib/markdown";
import PageText from "../PageText";
import { Alert, Badge, Dialog, ErrorState, Skeleton, errorMessage, useToast } from "../ui";

/** One answer, whether it came back just now or was loaded from history. */
export interface AnswerView {
  id: number | null;
  text: string;
  abstained: boolean;
  abstentionReason: string;
  citations: Citation[];
  generative: boolean;
  modelName: string;
  durationMs: number;
  supportRatio: number;
  notes: string[];
  resolvedQuestion: string;
  planSteps: { question: string; abstained: boolean }[];
  unanswered: string[];
  suspectedInjections: number;
  retrieved: number;
  passagesUsed: number;
  reranker: string;
  semantic: boolean;
  timings: Record<string, number>;
}

export function fromResponse(response: ChatResponse): AnswerView {
  return {
    id: response.message_id,
    text: response.answer,
    abstained: response.abstained,
    abstentionReason: response.abstention_reason,
    citations: response.citations,
    generative: response.model.generative,
    modelName: response.model.name,
    durationMs: response.duration_ms,
    supportRatio: response.grounding.support_ratio,
    notes: response.notes,
    resolvedQuestion: response.resolved_question,
    planSteps: response.plan.planned
      ? response.plan.steps.map((step) => ({ question: step.question, abstained: step.abstained }))
      : [],
    unanswered: response.plan.unanswered,
    suspectedInjections: response.retrieval.suspected_injections,
    retrieved: response.retrieval.retrieved,
    passagesUsed: response.retrieval.passages_used,
    reranker: response.retrieval.reranker,
    semantic: response.retrieval.semantic,
    timings: response.timings ?? {},
  };
}

export function fromMessage(message: ChatMessage): AnswerView {
  const meta = message.metadata ?? {};
  const unanswered = meta.unanswered ?? [];
  return {
    id: message.id,
    text: message.content,
    abstained: message.abstained,
    abstentionReason: message.abstention_reason,
    citations: message.citations ?? [],
    generative: message.generative,
    modelName: message.model_name,
    durationMs: message.duration_ms,
    supportRatio: message.support_ratio,
    notes: meta.notes ?? [],
    resolvedQuestion: meta.followup?.rewritten ? meta.followup.resolved_question : "",
    planSteps: meta.planned
      ? (meta.plan ?? []).map((question) => ({
          question,
          abstained: unanswered.includes(question),
        }))
      : [],
    unanswered,
    suspectedInjections: meta.suspected_injections ?? 0,
    retrieved: message.retrieved,
    passagesUsed: message.passages_used,
    reranker: meta.reranker ?? "",
    semantic: Boolean(meta.semantic_retrieval),
    timings: meta.timings ?? {},
  };
}

// --- Source inspection --------------------------------------------------------

export function SourceDialog({
  citation,
  quote = "",
  onClose,
}: {
  citation: Citation | null;
  /** The answer that cited this, so its quoted sentences can be marked. */
  quote?: string;
  onClose: () => void;
}) {
  const source = useResource(
    () => api.source(citation!.document_id, citation!.chunk_id),
    [citation?.document_id, citation?.chunk_id],
    { enabled: Boolean(citation) },
  );

  return (
    <Dialog
      open={Boolean(citation)}
      onClose={onClose}
      variant="drawer"
      size="lg"
      title={citation ? `Source [${citation.marker}] · ${citation.document_title}` : "Source"}
    >
      {!citation ? null : source.loading && !source.data ? (
        <Skeleton lines={10} />
      ) : source.error ? (
        <ErrorState
          error={source.error}
          title="This source is no longer available to you"
        />
      ) : source.data ? (
        <div className="stack">
          <div className="badge-row">
            <Badge tone="neutral">
              Page {source.data.page_number} of {source.data.page_count}
            </Badge>
            <Badge tone="neutral">Version {source.data.version_number}</Badge>
            {source.data.extraction_method === "OCR" ? (
              <Badge tone="warning">Read by OCR</Badge>
            ) : null}
          </div>
          {!source.data.is_current ? (
            <Alert tone="warning" title="The document has changed since this answer">
              This is the text the answer was built from. The document has since been
              reprocessed or replaced by a newer version, so the current text may differ.
            </Alert>
          ) : null}
          <p className="muted small">
            The tinted passage is what was cited; the words the answer quoted are marked
            within it.
          </p>
          <PageText
            text={source.data.page_text}
            start={source.data.character_start}
            end={source.data.character_end}
            quotes={findQuotedSpans(quote, source.data.page_text, [
              toUtf16Offset(source.data.page_text, source.data.character_start),
              toUtf16Offset(source.data.page_text, source.data.character_end),
            ])}
            label={`Page ${source.data.page_number}`}
          />
          <Link
            className="link"
            // Offsets are only meaningful on the text they were cut from. When
            // the document has been reprocessed since, the link opens the page
            // without a highlight rather than marking whatever now sits at
            // those positions as the evidence.
            to={
              source.data.is_current
                ? `/documents/${source.data.document_id}?tab=text&page=${source.data.page_number}&start=${source.data.character_start}&end=${source.data.character_end}`
                : `/documents/${source.data.document_id}?tab=text&page=${source.data.page_number}`
            }
            onClick={onClose}
          >
            Open the document at this page
          </Link>
        </div>
      ) : null}
    </Dialog>
  );
}

// --- The answer itself ----------------------------------------------------------------

const RATINGS: [FeedbackRating, string][] = [
  ["HELPFUL", "Helpful"],
  ["NOT_HELPFUL", "Not helpful"],
  ["INCORRECT", "Incorrect"],
];

function Feedback({ messageId }: { messageId: number }) {
  const toast = useToast();
  const [rated, setRated] = useState<FeedbackRating | null>(null);
  const [pending, setPending] = useState(false);

  const rate = async (rating: FeedbackRating) => {
    setPending(true);
    try {
      await api.feedback(messageId, rating);
      setRated(rating);
    } catch (caught) {
      toast(errorMessage(caught), "danger");
    } finally {
      setPending(false);
    }
  };

  return (
    <div className="feedback" role="group" aria-label="Rate this answer">
      <span className="muted small">{rated ? "Thanks for the feedback." : "Was this useful?"}</span>
      {RATINGS.map(([rating, label]) => (
        <button
          key={rating}
          type="button"
          className={`chip${rated === rating ? " chip-active" : ""}`}
          aria-pressed={rated === rating}
          disabled={pending}
          onClick={() => rate(rating)}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

export function Answer({
  answer,
  onInspect,
}: {
  answer: AnswerView;
  onInspect: (citation: Citation, answerText: string) => void;
}) {
  const byMarker = new Map(answer.citations.map((citation) => [citation.marker, citation]));

  return (
    <div className="answer">
      {answer.resolvedQuestion ? (
        <p className="answer-resolved muted small">
          Read as a follow-up: searched for “{answer.resolvedQuestion}”.
        </p>
      ) : null}

      {answer.abstained ? (
        <div className="abstention">
          <p className="abstention-title">Not enough evidence to answer</p>
          <p>{explainAbstention(answer.abstentionReason)}</p>
          {answer.notes.filter((note) => note.includes("image")).map((note) => (
            <p key={note} className="muted small">
              {note}
            </p>
          ))}
        </div>
      ) : (
        <Markdown
          source={answer.text}
          renderCitation={(marker, key) => {
            const citation = byMarker.get(marker);
            if (!citation) return <sup key={key}>[{marker}]</sup>;
            return (
              <button
                key={key}
                type="button"
                className="citation"
                onClick={() => onInspect(citation, answer.text)}
                aria-label={`Source ${marker}: ${citation.document_title}, page ${citation.page_number}`}
              >
                {marker}
              </button>
            );
          }}
        />
      )}

      {answer.planSteps.length > 1 ? (
        <div className="plan">
          <p className="small strong">This question had {answer.planSteps.length} parts:</p>
          <ul>
            {answer.planSteps.map((step) => (
              <li key={step.question}>
                <Badge tone={step.abstained ? "muted" : "success"}>
                  {step.abstained ? "Not found" : "Answered"}
                </Badge>{" "}
                {step.question}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {answer.suspectedInjections > 0 ? (
        <Alert tone="warning">
          {answer.suspectedInjections} retrieved passage
          {answer.suspectedInjections === 1 ? "" : "s"} contained text that looked like
          instructions to an AI. It was treated as document content only, never as an
          instruction.
        </Alert>
      ) : null}

      {answer.citations.length ? (
        <div className="sources">
          <p className="sources-title">Sources</p>
          <ol className="source-list">
            {answer.citations.map((citation) => {
              const window = excerpt(citation.text, 0, Math.min(citation.text.length, 200), 0);
              return (
                <li key={citation.marker}>
                  <button type="button" className="source" onClick={() => onInspect(citation, answer.text)}>
                    <span className="source-marker">{citation.marker}</span>
                    <span className="source-body">
                      <span className="source-title">
                        {citation.document_title}
                        <span className="muted"> · page {citation.page_number}</span>
                      </span>
                      <span className="source-excerpt">
                        {window.text}
                        {window.clippedEnd || citation.text.length > 200 ? "…" : ""}
                      </span>
                    </span>
                  </button>
                </li>
              );
            })}
          </ol>
        </div>
      ) : null}

      <div className="answer-meta">
        <Badge
          tone={answer.generative ? "info" : "neutral"}
          title={
            answer.generative
              ? `Written by ${answer.modelName} and checked against the cited passages`
              : "Quoted verbatim from the documents; nothing was generated"
          }
        >
          {answer.generative ? "Written from sources" : "Quoted from sources"}
        </Badge>
        <span className="muted small">{formatDuration(answer.durationMs)}</span>
        <details className="answer-details">
          <summary>How this answer was produced</summary>
          <ul className="details-list">
            <li>
              {answer.retrieved} passages retrieved, {answer.passagesUsed} used
              {answer.semantic ? " (keyword and meaning search)" : " (keyword search)"}
            </li>
            {answer.reranker ? <li>Ranked by {answer.reranker}</li> : null}
            <li>Answered by {answer.modelName || "—"}</li>
            {!answer.abstained ? (
              <li>
                Every figure checked against the cited passages; wording support{" "}
                {formatPercent(answer.supportRatio)}
              </li>
            ) : null}
            {Object.keys(answer.timings).length ? (
              <li>
                Time:{" "}
                {Object.entries(answer.timings)
                  .map(([stage, ms]) => `${stage} ${formatDuration(ms)}`)
                  .join(", ")}
              </li>
            ) : null}
            {answer.notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        </details>
        {answer.id ? <Feedback messageId={answer.id} /> : null}
      </div>
    </div>
  );
}
