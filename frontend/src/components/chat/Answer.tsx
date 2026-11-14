import { useState, type FormEvent, type ReactNode } from "react";
import { Link } from "react-router-dom";

import { api } from "../../api/client";
import { useResource } from "../../api/hooks";
import type {
  AnswerSource,
  ChatMessage,
  ChatResponse,
  Citation,
  Degraded,
  FeedbackRating,
} from "../../api/types";
import { formatDuration, formatPercent } from "../../lib/format";
import { excerpt, findQuotedSpans, toUtf16Offset } from "../../lib/highlight";
import {
  abstentionTitle,
  explainAbstention,
  explainAnswererFailure,
  isAnswererFailure,
} from "../../lib/labels";
import { Markdown } from "../../lib/markdown";
import PageText from "../PageText";
import { Icon, type IconName } from "../icons";
import {
  Alert,
  Badge,
  Button,
  Dialog,
  ErrorState,
  Skeleton,
  errorMessage,
  plural,
  useToast,
} from "../ui";

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
  source: AnswerSource;
  /** The reader's earlier rating, so a reopened answer stays rated. */
  myFeedback?: FeedbackRating | null;
  /** What ran in a lesser mode: the AI at capacity, a failure, a simpler ranking. */
  degraded: Degraded | null;
  /** Questions to ask next, drawn from what was cited. */
  suggestions: string[];
}

export function fromResponse(response: ChatResponse): AnswerView {
  return {
    id: response.message_id,
    text: response.answer,
    abstained: response.abstained,
    abstentionReason: response.abstention_reason,
    citations: response.citations,
    generative: response.model.generative,
    modelName: response.model_display ?? response.model.name,
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
    source: response.source ?? (response.abstained ? "none" : "documents"),
    degraded: response.degraded ?? null,
    suggestions: response.suggestions ?? [],
  };
}

export function fromMessage(message: ChatMessage): AnswerView {
  const meta = message.metadata ?? {};
  const unanswered = meta.unanswered ?? [];
  return {
    myFeedback: message.my_feedback?.rating ?? null,
    id: message.id,
    text: message.content,
    abstained: message.abstained,
    abstentionReason: message.abstention_reason,
    citations: message.citations ?? [],
    generative: message.generative,
    modelName: message.model_display ?? message.model_name,
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
    source: meta.source ?? (message.abstained ? "none" : "documents"),
    degraded: message.degraded ?? meta.degraded ?? null,
    suggestions: meta.suggestions ?? [],
  };
}

/**
 * The answer as Markdown, its markers kept and resolved in footnotes:
 * "[1] Fee Circular 2025, p. 4". What Copy puts on the clipboard, so a
 * pasted answer still says where each claim came from.
 */
export function answerAsMarkdown(answer: AnswerView): string {
  if (answer.abstained) return `${abstentionTitle(answer.abstentionReason)}.`;
  const notes = answer.citations
    .slice()
    .sort((a, b) => a.marker - b.marker)
    .map((citation) => `[${citation.marker}] ${citation.document_title}, p. ${citation.page_number}`);
  const label =
    answer.source === "general" ? "\n\n(General knowledge, not from your documents.)" : "";
  return `${answer.text.trim()}${label}${notes.length ? `\n\n${notes.join("\n")}` : ""}`;
}

/** Distinct documents an answer cites. */
function citedDocuments(citations: Citation[]): number {
  return new Set(citations.map((citation) => citation.document_id)).size;
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
            <Badge tone="neutral" icon="book-open">
              Page {source.data.page_number} of {source.data.page_count}
            </Badge>
            <Badge tone="neutral" icon="layers">
              Version {source.data.version_number}
            </Badge>
            {source.data.extraction_method === "OCR" ? (
              <Badge tone="warning" icon="scan">
                Read by OCR
              </Badge>
            ) : null}
          </div>
          {!source.data.is_current ? (
            <Alert tone="warning" title="The document has changed since this answer">
              This is the text the answer was built from. The document has since been
              reprocessed or replaced by a newer version, so the current text may differ.
            </Alert>
          ) : null}
          <p className="panel-note">
            <Icon name="info" size={14} />
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
            className="link link-with-icon"
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
            <Icon name="arrow-right" size={14} />
          </Link>
        </div>
      ) : null}
    </Dialog>
  );
}

// --- The answer itself ----------------------------------------------------------------

const RATINGS: [FeedbackRating, string, IconName][] = [
  ["HELPFUL", "Helpful", "thumbs-up"],
  ["NOT_HELPFUL", "Not helpful", "thumbs-down"],
  ["INCORRECT", "Incorrect", "flag"],
];

function Feedback({ messageId, initial = null }: { messageId: number; initial?: FeedbackRating | null }) {
  const toast = useToast();
  const [rated, setRated] = useState<FeedbackRating | null>(initial);
  const [pending, setPending] = useState(false);
  // After a Not helpful or Incorrect, an optional "what was wrong?" - the
  // API has always taken a comment; nothing asked for one.
  const [asking, setAsking] = useState(false);
  const [comment, setComment] = useState("");
  const [sent, setSent] = useState(false);

  const rate = async (rating: FeedbackRating) => {
    setPending(true);
    try {
      await api.feedback(messageId, rating);
      setRated(rating);
      setSent(false);
      setAsking(rating !== "HELPFUL");
    } catch (caught) {
      toast(errorMessage(caught), "danger");
    } finally {
      setPending(false);
    }
  };

  const explain = async (event: FormEvent) => {
    event.preventDefault();
    if (!rated || !comment.trim()) return;
    setPending(true);
    try {
      await api.feedback(messageId, rated, comment.trim());
      setSent(true);
      setAsking(false);
      setComment("");
    } catch (caught) {
      toast(errorMessage(caught), "danger");
    } finally {
      setPending(false);
    }
  };

  return (
    <div className="feedback-wrap">
      <div className="feedback" role="group" aria-label="Rate this answer">
        <span className="feedback-label">
          {sent ? "Thanks - that helps us fix it." : rated ? "Thanks for the feedback." : "Was this useful?"}
        </span>
        {RATINGS.map(([rating, label, icon]) => (
          <button
            key={rating}
            type="button"
            className={`chip chip-sm${rated === rating ? " chip-active" : ""}`}
            title={label}
            aria-pressed={rated === rating}
            disabled={pending}
            onClick={() => rate(rating)}
          >
            <Icon name={icon} />
            <span className="chip-label">{label}</span>
          </button>
        ))}
      </div>
      {asking ? (
        <form className="feedback-comment" onSubmit={explain}>
          <label htmlFor={`feedback-${messageId}`} className="feedback-comment-label">
            What was wrong? <span className="muted">(optional)</span>
          </label>
          <textarea
            id={`feedback-${messageId}`}
            className="input"
            rows={2}
            maxLength={2000}
            value={comment}
            onChange={(event) => setComment(event.target.value)}
            placeholder="e.g. the fee shown is last year's"
          />
          <div className="feedback-comment-actions">
            <Button size="sm" variant="ghost" onClick={() => setAsking(false)}>
              Skip
            </Button>
            <Button size="sm" type="submit" loading={pending} disabled={!comment.trim()}>
              Send
            </Button>
          </div>
        </form>
      ) : null}
    </div>
  );
}

function DegradedNote({ degraded }: { degraded: Degraded | null }) {
  if (!degraded) return null;
  if (degraded.answerer) {
    return (
      <p className="answer-degraded" role="note">
        <Icon name="info" size={14} />
        {degraded.reason === "capacity"
          ? "AI writing is at capacity — this answer quotes your documents directly."
          : "AI writing is unavailable right now — this answer quotes your documents directly."}
      </p>
    );
  }
  if (degraded.reranker) {
    return (
      <p className="answer-degraded" role="note">
        <Icon name="info" size={14} />
        Passages were ranked in a simpler mode just now, so this answer may be less precise.
      </p>
    );
  }
  return null;
}

export function Answer({
  answer,
  onInspect,
  showDetails = false,
  actions,
  onAsk,
  offerAlternatives,
}: {
  answer: AnswerView;
  onInspect: (citation: Citation, answerText: string) => void;
  /** Engineering detail (ranking, support, stage timings, notes): system administrators. */
  showDetails?: boolean;
  /** Copy, Regenerate and the like, shown beside the rating. */
  actions?: ReactNode;
  /** Ask a suggested follow-up. Only the latest answer offers them. */
  onAsk?: (question: string) => void;
  /**
   * After an abstention: "Search instead" (within `collectionId`, the
   * collection the question was asked in), and "Ask across all documents"
   * when scoped.
   */
  offerAlternatives?: { searchFor: string; collectionId?: number; askEverywhere?: () => void };
}) {
  const byMarker = new Map(answer.citations.map((citation) => [citation.marker, citation]));
  const documents = citedDocuments(answer.citations);

  return (
    <div className="answer">
      {answer.resolvedQuestion ? (
        <p className="answer-resolved muted small">
          <Icon name="corner-down-left" size={13} />
          Read as a follow-up: searched for “{answer.resolvedQuestion}”.
        </p>
      ) : null}

      {answer.abstained ? (
        <div className="abstention">
          <Icon name="alert-triangle" size={18} />
          <div className="abstention-body">
            <p className="abstention-title">{abstentionTitle(answer.abstentionReason)}</p>
            <p>
              {isAnswererFailure(answer.abstentionReason)
                ? explainAnswererFailure(answer.degraded?.reason)
                : explainAbstention(answer.abstentionReason)}
            </p>
            {answer.notes.filter((note) => note.includes("image")).map((note) => (
              <p key={note} className="muted small">
                {note}
              </p>
            ))}
            {offerAlternatives ? (
              <div className="abstention-actions">
                <Link
                  className="btn btn-secondary btn-sm"
                  // Handed over in history state, which Search reads once
                  // and clears - never in the address, where a question
                  // lands in browser history and, on a reload, in access
                  // logs.
                  to="/search"
                  state={{
                    query: offerAlternatives.searchFor,
                    collectionId: offerAlternatives.collectionId,
                  }}
                >
                  <Icon name="search" />
                  Search instead
                </Link>
                {offerAlternatives.askEverywhere ? (
                  <Button size="sm" icon="globe" onClick={offerAlternatives.askEverywhere}>
                    Ask across all documents
                  </Button>
                ) : null}
              </div>
            ) : null}
          </div>
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

      {/* "This answer quotes your documents" is false of an answer that quoted
          nothing: the card above already says the AI could not write one. */}
      <DegradedNote
        degraded={answer.abstained && answer.degraded?.answerer ? null : answer.degraded}
      />

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
          <p className="sources-title">
            <Icon name="quote" size={13} />
            Sources
          </p>
          <ol className="source-list">
            {answer.citations.map((citation) => {
              const window = excerpt(citation.text, 0, Math.min(citation.text.length, 200), 0);
              return (
                <li key={citation.marker}>
                  <button type="button" className="source" onClick={() => onInspect(citation, answer.text)}>
                    <span className="source-marker">{citation.marker}</span>
                    <span className="source-body">
                      <span className="source-head">
                        <span className="source-title">{citation.document_title}</span>
                        <span className="source-page">Page {citation.page_number}</span>
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
        {answer.abstained ? null : answer.source === "general" ? (
          <Badge
            tone="warning"
            icon="globe"
            title="Your documents had nothing on this, so the answer comes from the model's general knowledge. It has no sources and was not checked against your documents."
          >
            General knowledge · not from your documents
          </Badge>
        ) : (
          <Badge
            tone={answer.generative ? "info" : "neutral"}
            icon={answer.generative ? "sparkles" : "quote"}
            title={
              answer.generative
                ? `Written by ${answer.modelName} and checked against the cited passages`
                : "Quoted verbatim from the documents; nothing was generated"
            }
          >
            {answer.generative ? "Written from sources" : "Quoted from sources"}
          </Badge>
        )}
        <span className="answer-summary">
          {answer.generative && answer.modelName && !answer.abstained ? (
            <span className="answer-model">{answer.modelName}</span>
          ) : null}
          {!answer.abstained && answer.source !== "general" && answer.citations.length ? (
            <span>
              Checked against {answer.citations.length} {plural(answer.citations.length, "source")}
              {documents > 1 ? ` in ${documents} documents` : ""}
            </span>
          ) : null}
          <span className="answer-duration">
            <Icon name="clock" size={13} />
            {formatDuration(answer.durationMs)}
          </span>
        </span>
        {actions ? <span className="answer-actions">{actions}</span> : null}
        {answer.id ? <Feedback messageId={answer.id} initial={answer.myFeedback ?? null} /> : null}
      </div>

      {onAsk && !answer.abstained && answer.suggestions.length ? (
        <div className="followups" role="group" aria-label="Suggested follow-up questions">
          {answer.suggestions.map((suggestion) => (
            <button key={suggestion} type="button" className="followup" onClick={() => onAsk(suggestion)}>
              <Icon name="corner-down-left" size={13} />
              {suggestion}
            </button>
          ))}
        </div>
      ) : null}

      {showDetails ? (
        <details className="answer-details">
          <summary>
            <Icon name="chevron-right" size={14} />
            Developer details
          </summary>
          <ul className="details-list">
            <li>
              {answer.retrieved} passages retrieved, {answer.passagesUsed} used
              {answer.semantic ? " (keyword and meaning search)" : " (keyword search)"}
            </li>
            {answer.reranker ? <li>Ranked by {answer.reranker}</li> : null}
            <li>{answer.generative ? `Answered by ${answer.modelName}` : answer.modelName || "Answered by —"}</li>
            {!answer.abstained && answer.source !== "general" ? (
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
      ) : null}
    </div>
  );
}
