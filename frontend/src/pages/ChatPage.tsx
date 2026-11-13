import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";

import { aiApi } from "../api/ai";
import { ApiError, api } from "../api/client";
import { useResource, type Resource } from "../api/hooks";
import type {
  Citation,
  Conversation,
  ConversationSummary,
  DocumentType,
  Paginated,
  StreamSource,
} from "../api/types";
import { useAuth, useMe } from "../auth/AuthContext";
import {
  Answer,
  SourceDialog,
  answerAsMarkdown,
  fromMessage,
  fromResponse,
  type AnswerView,
} from "../components/chat/Answer";
import { AnswerProgress, stageLabel } from "../components/chat/Progress";
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
import { TYPE_LABEL } from "../lib/labels";
import { Markdown } from "../lib/markdown";
import "../styles/chat.css";

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
  /** The step the server says it is on, and the passages it is reading. */
  stage?: string;
  sources?: StreamSource[];
  /** The reader pressed Stop before the answer arrived. */
  stopped?: boolean;
}

/** A conversation's turns as the server holds them. */
function turnsOf(conversation: Conversation): Turn[] {
  const turns: Turn[] = [];
  const messages = conversation.messages;
  for (let index = 0; index < messages.length; index += 1) {
    const message = messages[index];
    if (message.role !== "USER") continue;
    const reply = messages[index + 1];
    turns.push({
      key: `m${message.id}`,
      question: message.content,
      answer: reply && reply.role === "ASSISTANT" ? fromMessage(reply) : null,
    });
  }
  return turns;
}

/**
 * Whether `turns` show exactly the answers the server holds, in order. A
 * turn without an answer (stopped, failed) is the page's own: the server
 * never had it.
 */
function showsAnswersOf(turns: Turn[], conversation: Conversation): boolean {
  const shown = turns.flatMap((turn) => (turn.answer?.id ? [turn.answer.id] : []));
  const held = conversation.messages
    .filter((message) => message.role === "ASSISTANT")
    .map((message) => message.id);
  return shown.length === held.length && shown.every((id, index) => id === held[index]);
}

/** Why a turn got no answer, in words for the reader. */
function failureMessage(error: unknown): string {
  return error instanceof ApiError && error.status === 429
    ? "You have asked a lot of questions in a short time. Wait a few minutes and try again."
    : errorMessage(error);
}

/** How close to the bottom counts as "reading the latest" (px). */
const NEAR_BOTTOM = 80;

function clock(value: string): string {
  return new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" }).format(
    new Date(value),
  );
}

function isAbort(caught: unknown): boolean {
  return caught instanceof DOMException && caught.name === "AbortError";
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
  // A demo guest's conversations are kept until the demo is cleared: the
  // server refuses renaming and deleting them, so neither is offered.
  const manage = !useAuth().can("is_guest");
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
      {manage && loaded.length ? (
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
                {selecting || !manage ? null : (
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
  const me = useMe();
  const toast = useToast();
  const params = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const conversationId = params.conversationId ? Number(params.conversationId) : null;

  const handoff = (location.state ?? {}) as {
    question?: string;
    documentId?: number;
    documentTitle?: string;
    // From Search: the scope its results came from.
    collectionId?: number;
    documentType?: string;
    department?: string;
  };
  const [scope, setScope] = useState<{ id: number; title: string } | null>(
    handoff.documentId ? { id: handoff.documentId, title: handoff.documentTitle ?? "" } : null,
  );
  // Narrows retrieval to one collection (spec 14) when no single document
  // is in scope. The server re-applies the reader's permissions first.
  const collections = useResource(() => api.collections(), []);
  const [collectionId, setCollectionId] = useState(handoff.collectionId ? String(handoff.collectionId) : "");
  // A type or department a search was narrowed to, until cleared.
  const [narrowing, setNarrowing] = useState({
    document_type: handoff.documentType ?? "",
    department: handoff.department ?? "",
  });

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
  // The composer's text as last rendered, for a turn that ends after the
  // reader has typed something new there.
  const draftNow = useRef(draft);
  draftNow.current = draft;
  const [sending, setSending] = useState(false);
  // The turn being written, so Stop can end it (and the server stops the
  // model, see ChatStreamView).
  const inFlight = useRef<AbortController | null>(null);
  // Edit & resend: the last question, back in the composer, replacing its
  // turn when sent.
  const [editing, setEditing] = useState<{ key: string; replaces: number | null } | null>(null);
  // Enter while an answer is still being written: said, not ignored.
  const [busyHint, setBusyHint] = useState(false);
  // The one live region: what a screen reader hears about the turn.
  const [announcement, setAnnouncement] = useState("");
  // Whether answers are being written right now, or quoted (capacity).
  const [capabilitiesTick, setCapabilitiesTick] = useState(0);
  const capabilities = useResource(() => aiApi.capabilities(), [capabilitiesTick]);
  // Follow the newest words only while the reader is at the bottom.
  const following = useRef(true);
  const [showJump, setShowJump] = useState(false);
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
  // The turns on screen were painted from a remembered copy of the
  // conversation (api/cache.ts) that this visit's own load has not
  // confirmed yet.
  const [fromMemory, setFromMemory] = useState(false);
  // Counts the turns this page has started. What the server said about the
  // conversation before the latest of them cannot replace what is on screen.
  const started = useRef(0);
  // The edit in progress, while the turn it edits is on screen. Any other -
  // another conversation's, or a turn the server has since replaced - is
  // none: the composer's question then goes as a new one.
  const edit = editing && turns.some((turn) => turn.key === editing.key) ? editing : null;
  // Ends an edit, and takes the question it put in the composer with it -
  // but only while the edit was showing: otherwise what is there is the
  // reader's own.
  const dropEdit = () => {
    if (!editing) return;
    setEditing(null);
    if (edit) setDraft("");
  };

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
    setFromMemory(false);
    // An edit belongs to the conversation it was started in. Carried
    // along, it was sent here naming the other conversation's answer to
    // replace, and refused.
    dropEdit();
    if (conversationId === null || conversationId !== turnsFor) {
      // Another conversation's turns never sit under this URL while its
      // own history loads - or, if the load fails, for good.
      setTurns([]);
      if (conversationId === null) setTurnsFor(null);
    }
    // Only a change of conversation does anything here (`shown`), and it
    // re-runs the effect with that render's dropEdit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId, turnsFor]);

  useEffect(() => {
    if (conversationId === null || !history.data || history.data.id !== conversationId) return;
    if (turnsFor !== conversationId) {
      setTurns(turnsOf(history.data));
      setTurnsFor(conversationId);
      setFromMemory(history.stale);
      return;
    }
    // Painted from a remembered copy: this visit's own load replaces it
    // when it arrives. Another tab or device may have added a turn since,
    // and follow-ups are resolved against what the server holds.
    if (!fromMemory || history.stale) return;
    setFromMemory(false);
    const held = history.data;
    setTurns((current) => (showsAnswersOf(current, held) ? current : turnsOf(held)));
  }, [conversationId, history.data, history.stale, turnsFor, fromMemory]);

  // Autoscroll only for a reader who is already at the bottom. Every delta
  // changes `turns`, and scrolling on each one yanked anyone reading the
  // sources further up back down.
  useEffect(() => {
    const box = thread.current;
    if (!box) return;
    if (following.current) {
      box.scrollTo({ top: box.scrollHeight, behavior: sending ? "auto" : "smooth" });
      setShowJump(false);
    } else if (turns.length) {
      setShowJump(true);
    }
  }, [turns, sending]);

  const onThreadScroll = () => {
    const box = thread.current;
    if (!box) return;
    following.current = box.scrollHeight - box.scrollTop - box.clientHeight < NEAR_BOTTOM;
    if (following.current) setShowJump(false);
  };

  const jumpToLatest = () => {
    following.current = true;
    setShowJump(false);
    thread.current?.scrollTo({ top: thread.current.scrollHeight, behavior: "smooth" });
  };

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

  // What the server kept of a turn that ended without an answer: stopped,
  // or failed. It learns of a Stop only when writing to the closed
  // connection fails, so a turn stopped in its last moments can still have
  // been saved - and the answer it replaced removed. The page then shows
  // the conversation as the server holds it, not an answer that is gone or
  // a question it says has none. `restored`: an edit the stopped or failed
  // turn gave back to the composer.
  const reconcile = async (id: number, restored?: { question: string; replaces: number }) => {
    const askedIn = view.current;
    const turn = started.current;
    let held: Conversation;
    try {
      held = await api.conversation(id);
    } catch {
      return; // Best effort: the page stays as it is.
    }
    // Never over another conversation, or a turn started since.
    if (!mounted.current || view.current !== askedIn || started.current !== turn) return;
    setTurns((current) => (showsAnswersOf(current, held) ? current : turnsOf(held)));
    // Saved after all - the answer it was to replace is gone: the edit is
    // not left in the composer to be sent a second time.
    if (restored && !held.messages.some((message) => message.id === restored.replaces)) {
      setEditing((current) => (current?.replaces === restored.replaces ? null : current));
      setDraft((current) => (current === restored.question ? "" : current));
    }
  };

  const send = async (
    text: string,
    options: { replaceKey?: string; replaces?: number | null; everywhere?: boolean } = {},
  ) => {
    const question = text.trim();
    if (!question) return;
    if (sending) {
      setBusyHint(true);
      setAnnouncement("Still answering the last question.");
      return;
    }
    const key = `t${Date.now()}`;
    const askedIn = view.current;
    started.current += 1;
    const fresh: Turn = { key, question, answer: null };
    // The turn a regenerated or edited one replaces. The server removes it
    // only together with saving its replacement, so until then it is still
    // the conversation's, and it comes back if this turn is stopped or fails.
    const replaced = options.replaces
      ? turns.find((turn) => turn.key === options.replaceKey && turn.answer?.id === options.replaces)
      : undefined;
    // A regenerated, edited or retried turn takes the place of the one it
    // replaces; anything else is a new turn at the end.
    setTurns((current) =>
      options.replaceKey && current.some((turn) => turn.key === options.replaceKey)
        ? current.map((turn) => (turn.key === options.replaceKey ? fresh : turn))
        : [...current, fresh],
    );
    setDraft("");
    setEditing(null);
    // Started on remembered turns, which this visit's load of the
    // conversation then no longer replaces: it may predate this turn.
    setFromMemory(false);
    setBusyHint(false);
    setSending(true);
    following.current = true;
    setAnnouncement("Searching your documents.");
    const controller = new AbortController();
    inFlight.current = controller;
    const patch = (update: (turn: Turn) => Turn) =>
      setTurns((current) => current.map((turn) => (turn.key === key ? update(turn) : turn)));
    try {
      const response = await api.chatStream(
        {
          question,
          conversation_id: conversationId,
          ...(options.replaces ? { replaces: options.replaces } : {}),
          ...(options.everywhere
            ? {}
            : scope
              ? { document_id: scope.id }
              : collectionId
                ? { collection_id: Number(collectionId) }
                : {}),
          ...(options.everywhere || scope
            ? {}
            : {
                ...(narrowing.document_type ? { document_type: narrowing.document_type } : {}),
                ...(narrowing.department ? { department: narrowing.department } : {}),
              }),
        },
        {
          onDelta: (piece) => patch((turn) => ({ ...turn, streaming: (turn.streaming ?? "") + piece })),
          onReset: (source) =>
            patch((turn) => ({ ...turn, streaming: "", streamingSource: source })),
          onStatus: (stage) => patch((turn) => ({ ...turn, stage })),
          onSources: (sources) => patch((turn) => ({ ...turn, sources })),
        },
        controller.signal,
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
      setAnnouncement(response.abstained ? "No answer: not enough evidence." : "Answer ready.");
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
      if (!mounted.current) return;
      const stopped = isAbort(caught);
      if (view.current === askedIn) {
        let restored: { question: string; replaces: number } | undefined;
        if (replaced?.answer) {
          const replacedId = replaced.answer.id;
          setTurns((current) => current.map((turn) => (turn.key === key ? replaced : turn)));
          // An edited question goes back into the composer, so what was
          // typed is not lost - unless something new has been typed since.
          if (question !== replaced.question && !draftNow.current.trim()) {
            setDraft(question);
            setEditing({ key: replaced.key, replaces: replacedId });
            if (replacedId) restored = { question, replaces: replacedId };
          }
          if (!stopped) toast(failureMessage(caught), "danger");
        } else {
          patch((turn) => (stopped ? { ...turn, stopped: true } : { ...turn, error: caught }));
        }
        setAnnouncement(stopped ? "Answer stopped." : "The answer could not be completed.");
        if (conversationId !== null) void reconcile(conversationId, restored);
      }
      // Saved after all, perhaps: a first question with its conversation.
      conversations.reload();
    } finally {
      if (inFlight.current === controller) inFlight.current = null;
      setSending(false);
      // Writing may have just paused or resumed (a cooldown, the day's quota).
      setCapabilitiesTick((tick) => tick + 1);
      if (view.current === askedIn) composer.current?.focus();
    }
  };

  const stop = () => inFlight.current?.abort();

  const copy = async (answer: AnswerView) => {
    try {
      await navigator.clipboard.writeText(answerAsMarkdown(answer));
      toast("Answer copied, with its sources.", "success");
    } catch {
      toast("The answer could not be copied. Select it and copy it instead.", "danger");
    }
  };

  const sendDraft = () =>
    void send(draft, edit ? { replaceKey: edit.key, replaces: edit.replaces } : {});

  const submit = (event: FormEvent) => {
    event.preventDefault();
    sendDraft();
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      sendDraft();
    }
  };

  const editLast = (turn: Turn) => {
    setDraft(turn.question);
    setEditing({ key: turn.key, replaces: turn.answer?.id ?? null });
    composer.current?.focus();
  };

  const startNew = () => {
    setScope(null);
    // An edit is the open conversation's (see the effect above).
    dropEdit();
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
  // A conversation that could not be opened - also one painted from a
  // remembered copy that its own load then could not confirm (deleted in
  // another tab, say), whose turns must not stay standing.
  const unopened = Boolean(history.error) && (opening || fromMemory);
  const conversationCount = conversations.data?.count ?? 0;
  const lastKey = turns.length ? turns[turns.length - 1].key : null;
  const scoped = Boolean(scope || collectionId);
  const quoting = capabilities.data?.mode === "quoting";
  const showDetails = Boolean(me.capabilities.view_system_status);

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
        {/* The one live region. The thread itself is not live: with it and
            the streaming text both announcing, a screen reader read the
            answer out word by word, twice. */}
        <p className="visually-hidden" aria-live="polite">
          {announcement}
        </p>
        <div className="thread" ref={thread} aria-busy={sending} onScroll={onThreadScroll}>
          <div className="thread-inner">
            {unopened ? (
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
              turns.map((turn) => {
                const latest = turn.key === lastKey;
                const settled = !sending || !latest;
                return (
                  <article key={turn.key} className="turn">
                    <div className="user-row">
                      {latest && settled && !edit ? (
                        <IconButton
                          icon="pencil"
                          size="sm"
                          className="turn-edit"
                          label="Edit question"
                          onClick={() => editLast(turn)}
                        />
                      ) : null}
                      <div className={`bubble bubble-user${edit?.key === turn.key ? " bubble-editing" : ""}`}>
                        <p>{turn.question}</p>
                      </div>
                    </div>
                    <div className="assistant-row">
                      <span className="assistant-avatar" aria-hidden="true">
                        <Icon name="sparkles" size={16} />
                      </span>
                      <div className="bubble bubble-assistant">
                        {turn.answer ? (
                          <Answer
                            answer={turn.answer}
                            showDetails={showDetails}
                            onInspect={(citation, quote) => setInspecting({ citation, quote })}
                            onAsk={latest && settled ? (question) => void send(question) : undefined}
                            offerAlternatives={
                              latest && turn.answer.abstained
                                ? {
                                    searchFor: turn.question,
                                    // The collection asked within, which
                                    // Search can narrow to as well (one
                                    // document in scope, it cannot).
                                    collectionId:
                                      !scope && collectionId ? Number(collectionId) : undefined,
                                    askEverywhere: scoped
                                      ? () => {
                                          setScope(null);
                                          setCollectionId("");
                                          void send(turn.question, { everywhere: true });
                                        }
                                      : undefined,
                                  }
                                : undefined
                            }
                            actions={
                              <>
                                {turn.answer.abstained ? null : (
                                  <IconButton
                                    icon="copy"
                                    size="sm"
                                    label="Copy answer"
                                    onClick={() => void copy(turn.answer!)}
                                  />
                                )}
                                {latest && settled && turn.answer.id ? (
                                  <IconButton
                                    icon="refresh"
                                    size="sm"
                                    label="Regenerate answer"
                                    onClick={() =>
                                      void send(turn.question, {
                                        replaceKey: turn.key,
                                        replaces: turn.answer!.id,
                                      })
                                    }
                                  />
                                ) : null}
                              </>
                            }
                          />
                        ) : turn.error || turn.stopped ? (
                          <div className="turn-failed">
                            {turn.stopped ? (
                              // No claim that nothing was saved: a Stop can
                              // reach the server after it saved the turn.
                              // Then the page shows what it saved instead
                              // (reconcile).
                              <p className="turn-stopped">
                                <Icon name="x-circle" size={15} />
                                You stopped this answer.
                              </p>
                            ) : (
                              <Alert tone="danger" title="No answer">
                                {failureMessage(turn.error)}
                              </Alert>
                            )}
                            {settled ? (
                              <Button
                                size="sm"
                                icon="refresh"
                                onClick={() => void send(turn.question, { replaceKey: turn.key })}
                              >
                                Try again
                              </Button>
                            ) : null}
                          </div>
                        ) : turn.streaming ? (
                          // Provisional: the server's final answer replaces it.
                          <div className="answer answer-streaming" aria-busy="true">
                            <AnswerProgress stage={turn.stage} sources={turn.sources} compact />
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
                          <div className="thinking" aria-label={stageLabel(turn.stage)}>
                            <AnswerProgress stage={turn.stage} sources={turn.sources} />
                          </div>
                        )}
                      </div>
                    </div>
                  </article>
                );
              })
            )}
          </div>
        </div>
        {showJump ? (
          <button type="button" className="jump-latest" onClick={jumpToLatest}>
            <Icon name="arrow-up" size={14} className="jump-latest-icon" />
            Jump to latest
          </button>
        ) : null}

        <form className="composer" onSubmit={submit}>
          <div className="composer-inner">
            {edit ? (
              <div className="composer-editing">
                <Icon name="pencil" size={14} />
                <span>Editing your last question - sending replaces its answer.</span>
                <IconButton icon="x" size="sm" label="Cancel editing" onClick={dropEdit} />
              </div>
            ) : null}
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
                disabled={opening || unopened}
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
                  {!scope && (narrowing.document_type || narrowing.department) ? (
                    <span className="scope-chip">
                      <Icon name="filter" size={14} />
                      <span className="scope-chip-text">
                        Only{" "}
                        <strong>
                          {[
                            narrowing.document_type
                              ? (TYPE_LABEL[narrowing.document_type as DocumentType] ?? narrowing.document_type)
                              : "",
                            narrowing.department,
                          ]
                            .filter(Boolean)
                            .join(" · ")}
                        </strong>
                      </span>
                      <IconButton
                        icon="x"
                        size="sm"
                        label="Stop narrowing by type and department"
                        onClick={() => setNarrowing({ document_type: "", department: "" })}
                      />
                    </span>
                  ) : null}
                </div>
                {quoting ? (
                  <span
                    className="capacity-chip"
                    title="Every AI model is busy or has used today's allowance. Answers are quoted from your documents, still with their sources."
                  >
                    <Icon name="info" size={13} />
                    {capabilities.data?.resumes_at
                      ? `Answers quote your documents until ${clock(capabilities.data.resumes_at)}`
                      : "Answers quote your documents for now"}
                  </span>
                ) : null}
                {sending ? (
                  <Button type="button" size="sm" icon="x-circle" onClick={stop}>
                    Stop
                  </Button>
                ) : (
                  <Button
                    type="submit"
                    variant="primary"
                    size="sm"
                    icon="arrow-up"
                    disabled={!draft.trim() || opening || unopened}
                  >
                    {edit ? "Ask again" : "Ask"}
                  </Button>
                )}
              </div>
            </div>
            <p className="composer-hint">
              {busyHint ? (
                <span className="composer-busy">
                  Still answering your last question - wait for it, or press Stop.
                </span>
              ) : turns.length ? (
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
