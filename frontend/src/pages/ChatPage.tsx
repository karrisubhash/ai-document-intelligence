import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";

import { ApiError, api } from "../api/client";
import { useResource, type Resource } from "../api/hooks";
import type { Citation, ConversationSummary, Paginated } from "../api/types";
import {
  Answer,
  SourceDialog,
  fromMessage,
  fromResponse,
  type AnswerView,
} from "../components/chat/Answer";
import {
  Alert,
  Badge,
  Button,
  ConfirmDialog,
  Dialog,
  EmptyState,
  ErrorState,
  Skeleton,
  errorMessage,
  useDocumentTitle,
  useToast,
} from "../components/ui";
import { formatRelative } from "../lib/format";
import { Markdown } from "../lib/markdown";

interface Turn {
  key: string;
  question: string;
  answer: AnswerView | null;
  error?: unknown;
  /** The answer as it is being written; replaced by `answer` when done. */
  streaming?: string;
  /** Where the answer being written comes from, when the server said. */
  streamingSource?: string;
}

const SUGGESTIONS = [
  "What is the minimum attendance required to sit the end-semester examination?",
  "How long do I have to apply for revaluation?",
  "What is the hostel curfew on Saturdays?",
];

function ConversationList({
  activeId,
  conversations,
  onNew,
  onChanged,
  onMore,
}: {
  activeId: number | null;
  conversations: Resource<Paginated<ConversationSummary>>;
  onNew: () => void;
  onChanged: () => void;
  onMore: () => void;
}) {
  const navigate = useNavigate();
  const toast = useToast();
  const [renaming, setRenaming] = useState<ConversationSummary | null>(null);
  const [deleting, setDeleting] = useState<ConversationSummary | null>(null);
  const [title, setTitle] = useState("");
  const [pending, setPending] = useState(false);

  return (
    <aside className="conversations" aria-label="Conversations">
      <Button variant="primary" className="btn-block" onClick={onNew}>
        New question
      </Button>
      {conversations.loading && !conversations.data ? (
        <Skeleton lines={5} />
      ) : conversations.error ? (
        <ErrorState error={conversations.error} onRetry={conversations.reload} />
      ) : conversations.data?.results.length ? (
        <ul className="conversation-list">
          {conversations.data.results.map((conversation) => (
            <li
              key={conversation.id}
              className={`conversation${conversation.id === activeId ? " active" : ""}`}
            >
              <Link to={`/ask/${conversation.id}`} className="conversation-link">
                <span className="conversation-title">
                  {conversation.title || "Untitled conversation"}
                </span>
                <span className="conversation-meta">{formatRelative(conversation.updated_at)}</span>
              </Link>
              <div className="conversation-actions">
                <button
                  type="button"
                  className="icon-button icon-button-sm"
                  aria-label={`Rename ${conversation.title || "conversation"}`}
                  onClick={() => {
                    setTitle(conversation.title);
                    setRenaming(conversation);
                  }}
                >
                  ✎
                </button>
                <button
                  type="button"
                  className="icon-button icon-button-sm"
                  aria-label={`Delete ${conversation.title || "conversation"}`}
                  onClick={() => setDeleting(conversation)}
                >
                  🗑
                </button>
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted small">Your conversations will appear here.</p>
      )}
      {conversations.data && conversations.data.results.length < conversations.data.count ? (
        <Button variant="ghost" size="sm" onClick={onMore} loading={conversations.loading}>
          Show older conversations
        </Button>
      ) : null}

      <Dialog
        open={Boolean(renaming)}
        onClose={() => setRenaming(null)}
        title="Rename conversation"
        size="sm"
        footer={
          <>
            <Button variant="ghost" onClick={() => setRenaming(null)}>
              Cancel
            </Button>
            <Button
              variant="primary"
              loading={pending}
              disabled={!title.trim()}
              onClick={async () => {
                if (!renaming) return;
                setPending(true);
                try {
                  await api.renameConversation(renaming.id, title.trim());
                  setRenaming(null);
                  onChanged();
                } catch (caught) {
                  toast(errorMessage(caught), "danger");
                } finally {
                  setPending(false);
                }
              }}
            >
              Save
            </Button>
          </>
        }
      >
        <label className="field-label" htmlFor="conversation-title">
          Title
        </label>
        <input
          id="conversation-title"
          className="input"
          value={title}
          maxLength={255}
          onChange={(event) => setTitle(event.target.value)}
        />
      </Dialog>

      <ConfirmDialog
        open={Boolean(deleting)}
        title="Delete this conversation?"
        confirmLabel="Delete"
        pending={pending}
        onClose={() => setDeleting(null)}
        onConfirm={async () => {
          if (!deleting) return;
          setPending(true);
          try {
            await api.deleteConversation(deleting.id);
            if (deleting.id === activeId) navigate("/ask", { replace: true });
            setDeleting(null);
            onChanged();
          } catch (caught) {
            toast(errorMessage(caught), "danger");
          } finally {
            setPending(false);
          }
        }}
      >
        <p>The questions and answers in it are removed. The documents are not affected.</p>
      </ConfirmDialog>
    </aside>
  );
}

export default function ChatPage() {
  useDocumentTitle("Ask");
  const params = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const conversationId = params.conversationId ? Number(params.conversationId) : null;

  const handoff = (location.state ?? {}) as {
    question?: string;
    documentId?: number;
    documentTitle?: string;
  };
  const [scope, setScope] = useState<{ id: number; title: string } | null>(
    handoff.documentId ? { id: handoff.documentId, title: handoff.documentTitle ?? "" } : null,
  );
  // Narrows retrieval to one collection (spec 14) when no single document
  // is in scope. The server re-applies the reader's permissions first.
  const collections = useResource(() => api.collections(), []);
  const [collectionId, setCollectionId] = useState("");

  // Every page loaded so far, newest first. Only the first 50 were ever
  // reachable before; older conversations simply could not be opened.
  const [conversationPages, setConversationPages] = useState(1);
  const conversations = useResource(async () => {
    const first = await api.conversations(1);
    const results = [...first.results];
    for (let page = 2; page <= conversationPages && results.length < first.count; page += 1) {
      results.push(...(await api.conversations(page)).results);
    }
    return { ...first, results };
  }, [conversationPages]);
  const history = useResource(() => api.conversation(conversationId!), [conversationId], {
    enabled: conversationId !== null,
  });

  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState(handoff.question ?? "");
  const [sending, setSending] = useState(false);
  const [inspecting, setInspecting] = useState<{ citation: Citation; quote: string } | null>(
    null,
  );
  const thread = useRef<HTMLDivElement>(null);
  const composer = useRef<HTMLTextAreaElement>(null);
  // Which conversation the turns on screen belong to. A conversation this
  // page has just created already shows its turns with their live response
  // data, so it is not rebuilt from history; any OTHER conversation is.
  const [turnsFor, setTurnsFor] = useState<number | null>(null);

  // The conversation the route shows, and a counter that moves whenever it
  // REALLY changes (not when StrictMode re-runs an effect). An answer that
  // arrives after the user has moved to another conversation, or pressed
  // "New question", belongs to the view it was asked in: it used to adopt
  // whatever was on screen as its new conversation, and its follow-up was
  // then sent to the wrong one.
  const shown = useRef(conversationId);
  const view = useRef(0);

  useEffect(() => {
    if (shown.current === conversationId) return;
    shown.current = conversationId;
    view.current += 1;
    if (conversationId === null || conversationId !== turnsFor) {
      // Another conversation's turns never sit under this URL while its
      // own history loads - or, if the load fails, for good.
      setTurns([]);
      if (conversationId === null) setTurnsFor(null);
    }
  }, [conversationId, turnsFor]);

  useEffect(() => {
    if (conversationId === null || turnsFor === conversationId) return;
    if (!history.data || history.data.id !== conversationId) return;
    const rebuilt: Turn[] = [];
    const messages = history.data.messages;
    for (let index = 0; index < messages.length; index += 1) {
      const message = messages[index];
      if (message.role !== "USER") continue;
      const reply = messages[index + 1];
      rebuilt.push({
        key: `m${message.id}`,
        question: message.content,
        answer: reply && reply.role === "ASSISTANT" ? fromMessage(reply) : null,
      });
    }
    setTurns(rebuilt);
    setTurnsFor(conversationId);
  }, [conversationId, history.data, turnsFor]);

  useEffect(() => {
    thread.current?.scrollTo({ top: thread.current.scrollHeight, behavior: "smooth" });
  }, [turns]);

  // A question handed over from the dashboard or search is sent at once -
  // and cleared from the history entry FIRST, or a reload or a remount of
  // this page would send it again.
  const sentHandoff = useRef(false);
  useEffect(() => {
    if (handoff.question && !sentHandoff.current && conversationId === null) {
      sentHandoff.current = true;
      navigate(location.pathname, { replace: true, state: {} });
      void send(handoff.question);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const send = async (text: string) => {
    const question = text.trim();
    if (!question || sending) return;
    const key = `t${Date.now()}`;
    const askedIn = view.current;
    setTurns((current) => [...current, { key, question, answer: null }]);
    setDraft("");
    setSending(true);
    try {
      const patch = (update: (turn: Turn) => Turn) =>
        setTurns((current) => current.map((turn) => (turn.key === key ? update(turn) : turn)));
      const response = await api.chatStream(
        {
          question,
          conversation_id: conversationId,
          ...(scope
            ? { document_id: scope.id }
            : collectionId
              ? { collection_id: Number(collectionId) }
              : {}),
        },
        {
          onDelta: (piece) => patch((turn) => ({ ...turn, streaming: (turn.streaming ?? "") + piece })),
          onReset: (source) =>
            patch((turn) => ({ ...turn, streaming: "", streamingSource: source })),
        },
      );
      if (view.current !== askedIn) {
        // Saved in its own conversation, which the list now shows; not
        // displayed over the one the user moved to.
        conversations.reload();
        return;
      }
      setTurns((current) =>
        current.map((turn) => (turn.key === key ? { ...turn, answer: fromResponse(response) } : turn)),
      );
      if (conversationId === null) {
        setTurnsFor(response.conversation_id);
        navigate(`/ask/${response.conversation_id}`, {
          replace: true,
          // Same screen, new URL: focus stays in the composer (Layout).
          state: { keepFocus: true },
        });
      }
      conversations.reload();
    } catch (caught) {
      if (view.current === askedIn) {
        setTurns((current) =>
          current.map((turn) => (turn.key === key ? { ...turn, error: caught } : turn)),
        );
      }
    } finally {
      setSending(false);
      if (view.current === askedIn) composer.current?.focus();
    }
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void send(draft);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void send(draft);
    }
  };

  const startNew = () => {
    setScope(null);
    if (conversationId === null) {
      // Already on the new-question screen: clearing it is a new view too,
      // so an answer still on its way is not shown on the empty page.
      view.current += 1;
      setTurns([]);
    }
    navigate("/ask");
    composer.current?.focus();
  };

  // Showing a conversation whose turns have not been loaded yet.
  const opening = conversationId !== null && turnsFor !== conversationId;

  return (
    <div className="chat">
      <ConversationList
        activeId={conversationId}
        conversations={conversations}
        onNew={startNew}
        onChanged={conversations.reload}
        onMore={() => setConversationPages((pages) => pages + 1)}
      />

      <section className="chat-main" aria-label="Conversation">
        <div className="thread" ref={thread} aria-live="polite" aria-busy={sending}>
          {history.error && opening ? (
            <ErrorState
              error={history.error}
              title="This conversation could not be opened"
            />
          ) : opening ? (
            <Skeleton lines={6} />
          ) : !turns.length ? (
            <EmptyState title="Ask a question about your documents" icon="✦">
              <p>
                Answers are built only from documents you are allowed to read, and every claim
                cites the passage it came from. When the documents do not say, you will be told
                so instead of getting a guess.
              </p>
              <div className="suggestions">
                {SUGGESTIONS.map((suggestion) => (
                  <button
                    key={suggestion}
                    type="button"
                    className="chip"
                    onClick={() => void send(suggestion)}
                  >
                    {suggestion}
                  </button>
                ))}
              </div>
            </EmptyState>
          ) : (
            turns.map((turn) => (
              <article key={turn.key} className="turn">
                <div className="bubble bubble-user">
                  <p>{turn.question}</p>
                </div>
                <div className="bubble bubble-assistant">
                  {turn.answer ? (
                    <Answer
                      answer={turn.answer}
                      onInspect={(citation, quote) => setInspecting({ citation, quote })}
                    />
                  ) : turn.error ? (
                    <Alert tone="danger" title="No answer">
                      {turn.error instanceof ApiError && turn.error.status === 429
                        ? "You have asked a lot of questions in a short time. Wait a few minutes and try again."
                        : errorMessage(turn.error)}
                    </Alert>
                  ) : turn.streaming ? (
                    // Provisional: the server's final answer replaces it.
                    <div className="answer answer-streaming" aria-live="polite" aria-busy="true">
                      <Markdown
                        source={turn.streaming}
                        renderCitation={(marker, key) => <sup key={key}>[{marker}]</sup>}
                      />
                      {turn.streamingSource === "general" ? (
                        <div className="answer-meta">
                          <Badge tone="warning">General knowledge · not from your documents</Badge>
                        </div>
                      ) : null}
                    </div>
                  ) : (
                    <div className="thinking">
                      <span className="spinner" aria-hidden="true" />
                      <span>Searching your documents and checking the evidence…</span>
                    </div>
                  )}
                </div>
              </article>
            ))
          )}
        </div>

        <form className="composer" onSubmit={submit}>
          {scope ? (
            <div className="scope">
              <span>
                Asking about <strong>{scope.title || `document ${scope.id}`}</strong> only
              </span>
              <button type="button" className="link-button" onClick={() => setScope(null)}>
                Search all documents
              </button>
            </div>
          ) : collections.data?.results.length ? (
            <div className="scope">
              <label htmlFor="composer-collection">Ask within</label>
              <select
                id="composer-collection"
                className="input"
                value={collectionId}
                onChange={(event) => setCollectionId(event.target.value)}
              >
                <option value="">All documents</option>
                {collections.data.results.map((collection) => (
                  <option key={collection.id} value={collection.id}>
                    {collection.name}
                  </option>
                ))}
              </select>
            </div>
          ) : null}
          <label htmlFor="composer" className="visually-hidden">
            Your question
          </label>
          <div className="composer-row">
            <textarea
              id="composer"
              ref={composer}
              className="input composer-input"
              rows={2}
              maxLength={2000}
              placeholder="Ask a question… (Enter to send, Shift+Enter for a new line)"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={onKeyDown}
              disabled={opening}
            />
            <Button
              type="submit"
              variant="primary"
              loading={sending}
              disabled={!draft.trim() || opening}
            >
              Ask
            </Button>
          </div>
          <p className="composer-hint muted small">
            Follow-up questions like “what about MECH?” use this conversation for context.
          </p>
        </form>
      </section>

      <SourceDialog
        citation={inspecting?.citation ?? null}
        quote={inspecting?.quote}
        onClose={() => setInspecting(null)}
      />
    </div>
  );
}
