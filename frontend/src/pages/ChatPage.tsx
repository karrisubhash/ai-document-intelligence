import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import { DOCUMENT_TYPES, type ChatResponse } from "../api/types";
import { useToken } from "../auth/AuthContext";
import { Banner, Empty, ErrorBox, Excerpt, Field, Pill } from "../components/ui";

/** Renders [1]-style citation markers as something the eye can follow. */
function AnswerText({ text }: { text: string }) {
  const parts = text.split(/(\[\d+\])/g);

  return (
    <p className="answer">
      {parts.map((part, index) =>
        /^\[\d+\]$/.test(part) ? (
          <span key={index} className="marker">
            {part}
          </span>
        ) : (
          <span key={index}>{part}</span>
        ),
      )}
    </p>
  );
}

function Turn({ turn }: { turn: ChatResponse }) {
  return (
    <div className="panel">
      <div className="question">{turn.question}</div>

      {turn.abstained ? (
        <Banner tone="warn" title="Abstained">
          {turn.abstention_reason || "No reason recorded."}
          <div className="small">
            An abstention is a successful outcome: the alternative is a confident
            answer with nothing behind it.
          </div>
        </Banner>
      ) : null}

      <AnswerText text={turn.answer} />

      <div className="chips">
        <Pill tone={turn.model.generative ? "info" : "muted"}>
          {turn.model.generative
            ? `generative · ${turn.model.name} ${turn.model.version}`
            : `extractive · ${turn.model.name} ${turn.model.version}`}
        </Pill>
        <Pill tone={turn.retrieval.semantic ? "ok" : "warn"}>
          {turn.retrieval.semantic ? "semantic retrieval" : "lexical retrieval"}
        </Pill>
        <Pill tone={turn.grounding.support_ratio >= 0.5 ? "ok" : "warn"}>
          support {(turn.grounding.support_ratio * 100).toFixed(0)}%
        </Pill>
        {turn.retrieval.suspected_injections > 0 ? (
          <Pill tone="bad" title="Passages matching prompt-injection patterns">
            {turn.retrieval.suspected_injections} suspected injection(s)
          </Pill>
        ) : null}
        <Pill tone="muted">{turn.duration_ms} ms</Pill>
      </div>

      {!turn.model.generative ? (
        <p className="muted small">
          The bundled answerer quotes retrieved passages verbatim rather than writing
          prose. It cannot hallucinate, and it cannot synthesise across passages
          either.
        </p>
      ) : null}

      <div className="fields">
        <Field label="Retrieved">{turn.retrieval.retrieved}</Field>
        <Field label="Reranked">{turn.retrieval.reranked}</Field>
        <Field label="Passages used">{turn.retrieval.passages_used}</Field>
        <Field label="Reranker">{turn.retrieval.reranker || "—"}</Field>
      </div>

      {turn.grounding.failures.length > 0 ? (
        <Banner tone="bad" title="Grounding failures">
          <ul className="notes">
            {turn.grounding.failures.map((failure) => (
              <li key={failure}>{failure}</li>
            ))}
          </ul>
        </Banner>
      ) : null}

      {turn.citations.length > 0 ? (
        <div className="citations">
          <h4>Citations</h4>
          {turn.citations.map((citation) => (
            <div key={citation.chunk_id} className="citation">
              <div className="panel-head">
                <div>
                  <span className="marker">[{citation.marker}]</span>{" "}
                  <Link to={`/documents/${citation.document_id}`}>
                    {citation.document_title}
                  </Link>
                  <span className="muted small">
                    {" "}
                    · page {citation.page_number} · chars {citation.character_start}–
                    {citation.character_end}
                  </span>
                </div>
                <Pill tone="muted">relevance {citation.relevance.toFixed(3)}</Pill>
              </div>
              <Excerpt text={citation.text} limit={400} />
            </div>
          ))}
        </div>
      ) : (
        <Empty>No citations — nothing in the answer points at a stored passage.</Empty>
      )}

      {turn.notes.length > 0 ? (
        <ul className="notes">
          {turn.notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

export default function ChatPage() {
  const token = useToken();

  const [question, setQuestion] = useState("");
  const [documentType, setDocumentType] = useState("");
  const [department, setDepartment] = useState("");
  const [conversationId, setConversationId] = useState<number | null>(null);
  const [turns, setTurns] = useState<ChatResponse[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);

    try {
      const answer = await api.chat(token, {
        question,
        conversation_id: conversationId,
        document_type: documentType,
        department,
      });

      setConversationId(answer.conversation_id);
      setTurns((current) => [answer, ...current]);
      setQuestion("");
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack">
      <form className="card" onSubmit={submit}>
        <div className="card-head">
          <h2>Ask</h2>
          <div className="card-head-right">
            {conversationId ? (
              <Pill tone="muted">conversation #{conversationId}</Pill>
            ) : null}
            <button
              type="button"
              className="button button-quiet"
              disabled={busy || conversationId === null}
              onClick={() => {
                setConversationId(null);
                setTurns([]);
              }}
            >
              New conversation
            </button>
          </div>
        </div>

        <p className="muted">
          Retrieval → reranking → context → answer, over the documents you are
          authorized to read. Every answer carries its citations, its grounding
          check, and what produced it.
        </p>

        <label>
          <span>Question</span>
          <textarea
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            rows={3}
            placeholder="What is the minimum attendance requirement?"
            required
          />
        </label>

        <div className="row">
          <label>
            <span>Type</span>
            <select
              value={documentType}
              onChange={(event) => setDocumentType(event.target.value)}
            >
              <option value="">any</option>
              {DOCUMENT_TYPES.map((type) => (
                <option key={type} value={type}>
                  {type}
                </option>
              ))}
            </select>
          </label>

          <label>
            <span>Department</span>
            <input
              value={department}
              onChange={(event) => setDepartment(event.target.value)}
              placeholder="any"
            />
          </label>
        </div>

        <ErrorBox error={error} />

        <button className="button" type="submit" disabled={busy || !question.trim()}>
          {busy ? "Thinking…" : "Ask"}
        </button>
      </form>

      {turns.length === 0 ? (
        <div className="card">
          <Empty>No questions asked in this conversation yet.</Empty>
        </div>
      ) : (
        <div className="stack">
          {turns.map((turn) => (
            <Turn key={turn.message_id} turn={turn} />
          ))}
        </div>
      )}
    </div>
  );
}
