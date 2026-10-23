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
import { Icon } from "../components/icons";
import {
  Alert,
  Badge,
  Button,
  ConfirmDialog,
  Dialog,
  ErrorState,
  IconButton,
  Kbd,
  SelectBox,
  Skeleton,
  errorMessage,
  plural,
  useDocumentTitle,
  useToast,
} from "../components/ui";
import { EXAMPLE_QUESTIONS } from "../lib/examples";
import { formatRelative } from "../lib/format";
import { Markdown } from "../lib/markdown";

function fitComposer(box: HTMLTextAreaElement | null) {
  if (!box) return;
  box.style.height = "auto";
  box.style.height = `${Math.min(box.scrollHeight, 220)}px`;
}

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

function ConversationList({
  id,
  activeId,
  conversations,
  onNew,
  onOpen,
  onChanged,
  onMore,
}: {
  id: string;
  activeId: number | null;
  conversations: Resource<Paginated<ConversationSummary>>;
  onNew: () => void;
  /** A conversation in the list was chosen - also the one already open. */
  onOpen: () => void;
  onChanged: () => void;
  onMore: () => void;
}) {
  const navigate = useNavigate();
  const toast = useToast();
  const [renaming, setRenaming] = useState<ConversationSummary | null>(null);
  const [deleting, setDeleting] = useState<ConversationSummary | null>(null);
  const [title, setTitle] = useState("");
  const [pending, setPending] = useState(false);

  // Multi-select for deleting several at once. `all` means every one of the
  // user's conversations, including older ones not loaded yet.
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<Set<number>>(() => new Set());
  const [all, setAll] = useState(false);
  const [confirmingBulk, setConfirmingBulk] = useState(false);
  const [bulkError, setBulkError] = useState<unknown>(null);
  const modeButton = useRef<HTMLButtonElement>(null);
  const loaded = conversations.data?.results ?? [];
  const total = conversations.data?.count ?? 0;
  const chosen = all ? total : selected.size;
  const isChosen = (id: number) => all || selected.has(id);
  const stopSelecting = () => {
    setSelecting(false);
    setSelected(new Set());
    setAll(false);
  };
  const toggle = (id: number, on: boolean) => {
    // Unticking one while "all" is on leaves every LOADED one but it.
    const base = all ? new Set(loaded.map((item) => item.id)) : new Set(selected);
    if (on) base.add(id);
    else base.delete(id);
    setAll(false);
    setSelected(base);
  };

  const deleteChosen = async () => {
    setPending(true);
    setBulkError(null);
    // Outside the try: what was deleted before a failure is still gone.
    const deleted: number[] = [];
    try {
      if (all && loaded.length < total) {
        // Older ones are not loaded: the server deletes them in rounds,
        // within the snapshot of the first answer.
        let asOf: string | undefined;
        for (;;) {
          const result = await api.bulkDeleteConversations({ all: true, as_of: asOf });
          deleted.push(...result.deleted);
          asOf = result.as_of;
          if (!result.remaining || result.deleted.length === 0) break;
        }
      } else {
        // Exactly the ones on screen - never one started since in another tab.
        const ids = all ? loaded.map((item) => item.id) : [...selected];
        for (let start = 0; start < ids.length; start += 100) {
          const result = await api.bulkDeleteConversations({ ids: ids.slice(start, start + 100) });
          deleted.push(...result.deleted);
        }
      }
      toast(`Deleted ${deleted.length} ${plural(deleted.length, "conversation")}.`, "success");
      setConfirmingBulk(false);
      stopSelecting();
      window.setTimeout(() => modeButton.current?.focus(), 0);
    } catch (caught) {
      // Shown in the dialog: a toast would sit under its backdrop.
      setBulkError(caught);
      setSelected((current) => new Set([...current].filter((id) => !deleted.includes(id))));
    } finally {
      setPending(false);
      if (activeId !== null && deleted.includes(activeId)) navigate("/ask", { replace: true });
      if (deleted.length) onChanged();
    }
  };

  return (
    <aside id={id} className="conversations" aria-label="Conversations">
      <Button variant="primary" icon="square-pen" className="btn-block" onClick={onNew}>
        New question
      </Button>
      {loaded.length ? (
        <div className={selecting ? "conversation-select-bar" : "conversation-select-idle"}>
          {selecting ? (
            <>
              <SelectBox
                label="Select all conversations"
                checked={all || (selected.size > 0 && loaded.every((item) => selected.has(item.id)))}
                indeterminate={!all && selected.size > 0 && !loaded.every((item) => selected.has(item.id))}
                onChange={(on) => {
                  setAll(on);
                  setSelected(new Set());
                }}
              />
              <span className="small" aria-live="polite">
                {all ? `All ${total}` : selected.size} selected
              </span>
              <Button
                variant="danger"
                size="sm"
                icon="trash"
                disabled={chosen === 0}
                onClick={() => {
                  setBulkError(null);
                  setConfirmingBulk(true);
                }}
              >
                Delete selected
              </Button>
            </>
          ) : null}
          {/* One button for both, so focus stays on it as the mode flips. */}
          <Button
            ref={modeButton}
            variant="ghost"
            size="sm"
            icon={selecting ? "check" : "list-checks"}
            onClick={selecting ? stopSelecting : () => setSelecting(true)}
          >
            {selecting ? "Done" : "Select conversations"}
          </Button>
        </div>
      ) : null}
      {conversations.loading && !conversations.data ? (
        <div className="conversations-empty">
          <Skeleton lines={5} />
        </div>
      ) : conversations.error ? (
        <ErrorState error={conversations.error} onRetry={conversations.reload} />
      ) : conversations.data?.results.length ? (
        <>
          <p className="conversations-label">Recent</p>
          <ul className="conversation-list">
            {conversations.data.results.map((conversation) => (
              <li
                key={conversation.id}
                // While selecting, the tint follows the ticks, not the open conversation.
                className={`conversation${
                  selecting
                    ? isChosen(conversation.id)
                      ? " selected"
                      : ""
                    : conversation.id === activeId
                      ? " active"
                      : ""
                }`}
              >
                {selecting ? (
                  <span className="conversation-select">
                    <SelectBox
                      label={`Select ${conversation.title || "Untitled conversation"}`}
                      checked={isChosen(conversation.id)}
                      onChange={(on) => toggle(conversation.id, on)}
                    />
                  </span>
                ) : null}
                <Link
                  to={`/ask/${conversation.id}`}
                  className="conversation-link"
                  aria-current={conversation.id === activeId ? "page" : undefined}
                  onClick={onOpen}
                >
                  <span className="conversation-title">
                    {conversation.title || "Untitled conversation"}
                  </span>
                  <span className="conversation-meta">{formatRelative(conversation.updated_at)}</span>
                </Link>
                {/* Not rendered while selecting: a class's display: flex
                    would override the hidden attribute. */}
                {selecting ? null : (
                  <div className="conversation-actions">
                    <IconButton
                      icon="pencil"
                      size="sm"
                      label={`Rename ${conversation.title || "conversation"}`}
                      onClick={() => {
                        setTitle(conversation.title);
                        setRenaming(conversation);
                      }}
                    />
                    <IconButton
                      icon="trash"
                      size="sm"
                      label={`Delete ${conversation.title || "conversation"}`}
                      onClick={() => setDeleting(conversation)}
                    />
                  </div>
                )}
              </li>
            ))}
          </ul>
        </>
      ) : (
        <p className="conversations-empty">Your conversations will appear here.</p>
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
        <div className="field">
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
        </div>
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

      <ConfirmDialog
        open={confirmingBulk}
        title={
          all
            ? `Delete all ${total} ${plural(total, "conversation")}?`
            : `Delete ${chosen} ${plural(chosen, "conversation")}?`
        }
        confirmLabel={all ? "Delete all" : "Delete"}
        pending={pending}
        error={bulkError}
        onClose={() => setConfirmingBulk(false)}
        onConfirm={() => void deleteChosen()}
      >
        <p>
          The questions and answers in {chosen === 1 ? "it are" : "them are"} removed permanently.
          The documents are not affected.
        </p>
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
  // Phones: whether the folded conversation list is showing. Choosing a
  // conversation folds it (onOpen, also for the one already open), and so
  // does arriving at another one - but not this page giving a new
  // question its id, which can happen while the list is open on purpose.
  const [listOpen, setListOpen] = useState(false);
  useEffect(() => {
    if (!(location.state as { keepFocus?: boolean } | null)?.keepFocus) setListOpen(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId]);
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
  // Quick find hands Ask a new question by REPLACING this page. An answer
  // the replaced page was still waiting for must then do nothing at all: it
  // used to move the URL back to its own conversation, out from under the
  // new question.
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

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

  // The composer grows with what is typed, up to its maximum height - and
  // refits when its width changes, which re-wraps the text.
  useEffect(() => fitComposer(composer.current), [draft]);
  useEffect(() => {
    const box = composer.current;
    if (!box || typeof ResizeObserver !== "function") return;
    let width = box.clientWidth;
    const observer = new ResizeObserver(() => {
      // Width only: refitting changes the height, which must not loop.
      if (box.clientWidth === width) return;
      width = box.clientWidth;
      fitComposer(box);
    });
    observer.observe(box);
    return () => observer.disconnect();
  }, []);

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
      if (!mounted.current) return;
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
  const conversationCount = conversations.data?.count ?? 0;

  return (
    <div className={`chat${listOpen ? " chat-list-open" : ""}`}>
      {/* Phones only (CSS): the list folds away above the thread, which
          then has the whole screen. */}
      <div className="chat-mobile-bar">
        <Button
          variant="ghost"
          size="sm"
          icon="history"
          iconEnd="chevron-down"
          className="chat-list-toggle"
          aria-expanded={listOpen}
          aria-controls="conversation-panel"
          onClick={() => setListOpen((open) => !open)}
        >
          Conversations
          {conversationCount ? <span className="tab-count">{conversationCount}</span> : null}
        </Button>
        <Button
          size="sm"
          icon="square-pen"
          onClick={() => {
            setListOpen(false);
            startNew();
          }}
        >
          New
        </Button>
      </div>

      <ConversationList
        id="conversation-panel"
        activeId={conversationId}
        conversations={conversations}
        onNew={() => {
          setListOpen(false);
          startNew();
        }}
        onOpen={() => setListOpen(false)}
        onChanged={conversations.reload}
        onMore={() => setConversationPages((pages) => pages + 1)}
      />

      <section className="chat-main" aria-label="Conversation">
        <h1 className="visually-hidden">Ask</h1>
        <div className="thread" ref={thread} aria-live="polite" aria-busy={sending}>
          <div className="thread-inner">
            {history.error && opening ? (
              <ErrorState
                error={history.error}
                title="This conversation could not be opened"
              />
            ) : opening ? (
              <Skeleton lines={6} />
            ) : !turns.length ? (
              <div className="chat-welcome">
                <span className="chat-welcome-mark" aria-hidden="true">
                  <Icon name="sparkles" size={26} />
                </span>
                <h2 className="chat-welcome-title">Ask a question about your documents</h2>
                <p className="chat-welcome-text">
                  Answers are built only from documents you are allowed to read, and every claim
                  cites the passage it came from. When the documents do not say, you will be told
                  so instead of getting a guess.
                </p>
                <div className="suggestions">
                  {EXAMPLE_QUESTIONS.map((suggestion) => (
                    <button
                      key={suggestion}
                      type="button"
                      className="suggestion"
                      onClick={() => void send(suggestion)}
                    >
                      <Icon name="message" />
                      {suggestion}
                    </button>
                  ))}
                </div>
              </div>
            ) : (
              turns.map((turn) => (
                <article key={turn.key} className="turn">
                  <div className="bubble bubble-user">
                    <p>{turn.question}</p>
                  </div>
                  <div className="assistant-row">
                    <span className="assistant-avatar" aria-hidden="true">
                      <Icon name="sparkles" size={16} />
                    </span>
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
                              <Badge tone="warning" icon="globe">
                                General knowledge · not from your documents
                              </Badge>
                            </div>
                          ) : null}
                        </div>
                      ) : (
                        <div className="thinking">
                          <span className="typing" aria-hidden="true">
                            <span />
                            <span />
                            <span />
                          </span>
                          <span>Searching your documents and checking the evidence…</span>
                        </div>
                      )}
                    </div>
                  </div>
                </article>
              ))
            )}
          </div>
        </div>

        <form className="composer" onSubmit={submit}>
          <div className="composer-inner">
            <div className="composer-box">
              <label htmlFor="composer" className="visually-hidden">
                Your question
              </label>
              <textarea
                id="composer"
                ref={composer}
                className="composer-input"
                rows={1}
                maxLength={2000}
                aria-describedby="composer-keys"
                placeholder={
                  turns.length ? "Ask a follow-up question…" : "Ask a question about your documents…"
                }
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                onKeyDown={onKeyDown}
                disabled={opening}
              />
              <div className="composer-bar">
                <div className="composer-scope">
                  {scope ? (
                    <span className="scope-chip">
                      <Icon name="file-text" size={14} />
                      <span className="scope-chip-text">
                        Asking about <strong>{scope.title || `document ${scope.id}`}</strong> only
                      </span>
                      <IconButton
                        icon="x"
                        size="sm"
                        label="Search all documents"
                        onClick={() => setScope(null)}
                      />
                    </span>
                  ) : collections.data?.results.length ? (
                    <>
                      <label htmlFor="composer-collection">
                        <Icon name="folder" size={14} />
                        Ask within
                      </label>
                      <select
                        id="composer-collection"
                        className="input input-sm"
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
                    </>
                  ) : null}
                </div>
                <Button
                  type="submit"
                  variant="primary"
                  size="sm"
                  icon="arrow-up"
                  loading={sending}
                  disabled={!draft.trim() || opening}
                >
                  Ask
                </Button>
              </div>
            </div>
            <p className="composer-hint">
              {turns.length ? (
                <>Follow-up questions like “what about MECH?” use this conversation for context.</>
              ) : (
                <>Answers cite the passages they come from.</>
              )}
              <span className="composer-keys" id="composer-keys">
                <Kbd>Enter</Kbd> to send · <Kbd>Shift</Kbd> + <Kbd>Enter</Kbd> for a new line
              </span>
            </p>
          </div>
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
