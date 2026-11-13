import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";

import { api } from "../api/client";
import { useAction, useResource } from "../api/hooks";
import {
  DOCUMENT_TYPES,
  type DocumentType,
  type SearchGroup,
  type SearchHit,
  type SearchNotice,
  type SearchPassage,
  type SearchResponse,
  type TextSpan,
} from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { FileTypeIcon, Icon, type IconName } from "../components/icons";
import {
  Alert,
  Badge,
  Button,
  EmptyState,
  ErrorState,
  PageHeader,
  Skeleton,
  useDocumentTitle,
} from "../components/ui";
import { formatRelative } from "../lib/format";
import { highlightTerms, passageSegments, unwrapLines } from "../lib/highlight";
import { TYPE_LABEL } from "../lib/labels";
import "../styles/search.css";

interface Filters {
  document_type: string;
  department: string;
  /** A collection id, as the select holds it; "" for any. */
  collection: string;
}

const NO_FILTERS: Filters = { document_type: "", department: "", collection: "" };

interface SearchRequest {
  query: string;
  document_type: string;
  department: string;
  collection_id: number | null;
  include_below_floor: boolean;
  group_by: "document";
}

function requestFor(query: string, filters: Filters): SearchRequest {
  return {
    query,
    document_type: filters.document_type,
    department: filters.department,
    collection_id: filters.collection ? Number(filters.collection) : null,
    include_below_floor: false,
    group_by: "document",
  };
}

const plural = (count: number, one: string, many = `${one}s`) => `${count} ${count === 1 ? one : many}`;

/** A passage as the page shows it; highlights are absent from older servers. */
type ShownPassage = Omit<SearchPassage, "highlights" | "snippet"> & {
  highlights?: TextSpan[];
  snippet?: TextSpan;
};
type ShownGroup = Omit<SearchGroup, "passages"> & { passages: ShownPassage[] };

function sourceLink(documentId: number, passage: { page_number: number; character_start: number; character_end: number }) {
  return `/documents/${documentId}?tab=text&page=${passage.page_number}&start=${passage.character_start}&end=${passage.character_end}`;
}

/**
 * The words the search matched, as the server marked them (its stemming,
 * its tokens) in a window around the first of them. A server too old to
 * send highlights gets the former client-side guess.
 */
function PassageText({
  text,
  highlights,
  snippet,
  query,
}: {
  text: string;
  highlights?: TextSpan[];
  snippet?: TextSpan;
  query: string;
}) {
  if (highlights === undefined) {
    const flowing = unwrapLines(text);
    const clipped = flowing.length > 420 ? `${flowing.slice(0, 420)}…` : flowing;
    return (
      <p className="snippet">
        {highlightTerms(clipped, query).map((segment, index) =>
          segment.highlighted ? <mark key={index}>{segment.text}</mark> : <span key={index}>{segment.text}</span>,
        )}
      </p>
    );
  }
  const { segments, clippedStart, clippedEnd } = passageSegments(text, highlights, snippet);
  return (
    <p className="snippet">
      {clippedStart ? "… " : null}
      {segments.map((segment, index) =>
        segment.highlighted ? <mark key={index}>{segment.text}</mark> : <span key={index}>{segment.text}</span>,
      )}
      {clippedEnd ? " …" : null}
    </p>
  );
}

type Strength = "strong" | "good" | "partial";
const STRENGTH_LABEL: Record<Strength, string> = {
  strong: "Strong match",
  good: "Good match",
  partial: "Partial match",
};

/**
 * How close a passage is, from the reranker's score against the floor the
 * server calibrated for it. Nothing when nothing was reranked or the floor
 * is unknown: a score from an uncalibrated model is not a strength.
 */
function strengthOf(relevance: number | null, floor: number | null): Strength | null {
  if (relevance === null || floor === null) return null;
  if (relevance >= 0.5) return "strong";
  if (relevance >= Math.max(0.1, floor * 3)) return "good";
  return "partial";
}

function MatchStrength({ relevance, floor }: { relevance: number | null; floor: number | null }) {
  const level = strengthOf(relevance, floor);
  if (!level) return null;
  const bars = level === "strong" ? 3 : level === "good" ? 2 : 1;
  return (
    <span className={`match-strength match-${level}`}>
      <span className="match-bars" aria-hidden="true">
        {[1, 2, 3].map((bar) => (
          <span key={bar} className={bar <= bars ? "on" : undefined} />
        ))}
      </span>
      {STRENGTH_LABEL[level]}
    </span>
  );
}

/** One card per document: its best passages, newest facts about it. */
function DocumentResult({ group, query, floor }: { group: ShownGroup; query: string; floor: number | null }) {
  const best = group.passages[0];
  return (
    <li className="result result-group">
      <FileTypeIcon fileName={group.file_name} />
      <div className="result-body">
        <div className="result-head">
          <Link className="result-title" to={sourceLink(group.document_id, best)}>
            {group.document_title}
          </Link>
          <MatchStrength relevance={group.relevance} floor={floor} />
        </div>
        {group.document_type || group.department || group.updated_at ? (
          <div className="result-meta">
            {group.document_type ? <Badge tone="neutral">{TYPE_LABEL[group.document_type]}</Badge> : null}
            {group.department ? (
              <span className="result-meta-item">
                <Icon name="building" size={13} />
                {group.department}
              </span>
            ) : null}
            {group.updated_at ? (
              <span className="result-meta-item">
                <Icon name="clock" size={13} />
                Updated {formatRelative(group.updated_at)}
              </span>
            ) : null}
          </div>
        ) : null}
        <ol className="result-passages">
          {group.passages.map((passage) => (
            <li key={passage.chunk_ids.join("-")} className="result-passage">
              <div className="result-passage-head">
                <Link
                  className="result-page"
                  to={sourceLink(group.document_id, passage)}
                  aria-label={`Page ${passage.page_number} of ${group.document_title}`}
                >
                  <Icon name="book-open" size={13} />
                  Page {passage.page_number}
                </Link>
                {passage.chunk_kind === "TABLE" ? <Badge tone="info">Table</Badge> : null}
              </div>
              <PassageText
                text={passage.text}
                highlights={passage.highlights}
                snippet={passage.snippet}
                query={query}
              />
            </li>
          ))}
        </ol>
        {group.more_passages ? (
          <p className="result-more">
            {plural(group.more_passages, "more passage")} in this document
          </p>
        ) : null}
      </div>
    </li>
  );
}

function passageOf(hit: SearchHit): ShownPassage {
  return {
    chunk_ids: [hit.chunk_id],
    page_number: hit.page_number,
    character_start: hit.character_start,
    character_end: hit.character_end,
    text: hit.text,
    highlights: hit.highlights,
    snippet: hit.snippet,
    relevance: hit.relevance,
    matched_by: hit.matched_by,
    chunk_kind: hit.chunk_kind ?? "",
  };
}

/** For a server that sent no `groups`: the same grouping, without merging. */
function groupsFromHits(hits: SearchHit[]): ShownGroup[] {
  const groups = new Map<number, ShownGroup>();
  const all = new Map<number, ShownPassage[]>();
  for (const hit of hits) {
    if (!groups.has(hit.document_id)) {
      groups.set(hit.document_id, {
        document_id: hit.document_id,
        document_title: hit.document_title,
        document_type: (hit.document_type ?? "") as DocumentType,
        department: hit.department ?? "",
        updated_at: hit.document_updated_at ?? "",
        file_name: hit.file_name ?? "",
        file_kind: hit.file_kind ?? "",
        version_number: hit.version_number ?? 0,
        relevance: hit.relevance,
        matched_by: hit.matched_by,
        passages: [],
        more_passages: 0,
      });
      all.set(hit.document_id, []);
    }
    all.get(hit.document_id)!.push(passageOf(hit));
  }
  return [...groups.values()].map((group) => {
    const passages = all.get(group.document_id)!;
    return { ...group, passages: passages.slice(0, 3), more_passages: Math.max(0, passages.length - 3) };
  });
}

/** A weaker match: one passage, shown plainly. */
function WeakHit({ hit, query }: { hit: SearchHit; query: string }) {
  return (
    <li className="result">
      <FileTypeIcon fileName={hit.file_name} />
      <div className="result-body">
        <div className="result-head">
          <Link className="result-title" to={sourceLink(hit.document_id, hit)}>
            {hit.document_title}
          </Link>
          <span className="result-page">
            <Icon name="book-open" size={13} />
            Page {hit.page_number}
          </span>
        </div>
        <PassageText text={hit.text} highlights={hit.highlights} snippet={hit.snippet} query={query} />
      </div>
    </li>
  );
}

interface NoticeView {
  tone: "info" | "warning";
  icon?: IconName;
  title: string;
  body: string;
  action?: { label: string; onClick: () => void };
}

/** Plain words for each notice the server can send; null: shown elsewhere. */
function describeNotice(
  notice: SearchNotice,
  actions: { clearFilters: () => void; goTo: (path: string) => void; canOperate: boolean },
): NoticeView | null {
  const documents = Number(notice.params.documents ?? 0);
  switch (notice.code) {
    case "NO_ACCESSIBLE_DOCUMENTS":
      return {
        tone: "info",
        title: "There is nothing to search yet",
        body: "Documents you upload, and documents shared with your department, become searchable once they have been processed.",
        action: { label: "Go to Documents", onClick: () => actions.goTo("/documents") },
      };
    case "FILTERS_EXCLUDE_ALL":
      return {
        tone: "info",
        title: "Your filters leave nothing to search",
        body: "None of the documents you can read matches every filter you chose.",
        action: { label: "Clear filters", onClick: actions.clearFilters },
      };
    case "NOTHING_INDEXED":
      return {
        tone: "info",
        title: "Your documents are not searchable yet",
        body: `${plural(documents, "document")} in this search ${documents === 1 ? "has" : "have"} not finished processing. Results appear once processing is done.`,
        action: { label: "View documents", onClick: () => actions.goTo("/documents") },
      };
    case "STALE_INDEX":
      return {
        tone: "warning",
        title: "Search is out of step with these documents",
        body: actions.canOperate
          ? "They need processing again before they can be searched. Check the Processing page."
          : "They need processing again before they can be searched. An administrator can fix this.",
        action: actions.canOperate
          ? { label: "Open Processing", onClick: () => actions.goTo("/admin/processing") }
          : undefined,
      };
    case "MEANING_UNAVAILABLE":
      return {
        tone: "warning",
        title: "Matching your words only",
        body: "Search by meaning is unavailable right now, so these results contain the words you typed. Passages that say it differently may be missing.",
      };
    case "RERANKER_UNAVAILABLE":
      return {
        tone: "warning",
        title: "Results are not ranked by relevance right now",
        body: "They are in the order they were found, so the closest match may not come first.",
      };
    default:
      // LEXICAL_BASELINE is the summary badge; NO_CLOSE_MATCH is the empty
      // state; codes this page does not know yet are left to the details.
      return null;
  }
}

export default function SearchPage() {
  useDocumentTitle("Search");
  const navigate = useNavigate();
  const location = useLocation();
  const { can } = useAuth();
  // Only a system administrator reads more than one department; for anyone
  // else a department filter could only offer departments that return
  // nothing, so it is not offered.
  const everyDepartment = can("view_system_status");
  const departments = useResource(() => api.departments(), [], { enabled: everyDepartment });
  const collections = useResource(() => api.collections(), []);

  // The query is deliberately NOT put in the URL. The search API takes it
  // by POST so that what people look for never lands in access logs or
  // browser history; mirroring it into the address bar would undo that.
  // Quick find hands a query over in history state instead, which this
  // page clears as soon as it has read it. A collection hand-off (from its
  // own "Search" button) carries no query - it only preselects the filter.
  const handoffState = (location.state as { query?: string; collectionId?: number } | null) ?? {};
  const handoff = (handoffState.query ?? "").trim();
  const [query, setQuery] = useState(handoff);
  const [filters, setFilters] = useState<Filters>({
    ...NO_FILTERS,
    collection: handoffState.collectionId ? String(handoffState.collectionId) : "",
  });
  // The server owns the page size (SEARCH_DEFAULT_LIMIT) and its ceiling
  // (SEARCH_MAX_LIMIT): a new search sends no limit, and "Show more" adds one
  // more page of the size the server chose. A fixed client-side size used to
  // be rejected outright by a server configured with a smaller maximum.
  const [pageSize, setPageSize] = useState(0);
  const [result, setResult] = useState<SearchResponse | null>(null);
  const [asked, setAsked] = useState("");
  // The search the results on screen came from. "Show more results" repeats
  // THAT one with a larger limit - it used to read the live inputs, so
  // editing the box or a filter without submitting replaced the list with a
  // different search.
  const [submitted, setSubmitted] = useState<SearchRequest | null>(null);
  // Only the latest search lands. A filter applies at once (changeFilters),
  // so a new search can start while the last is running, and a reranked
  // search can take longer than the one after it: the older answer, its
  // failure and its end of "busy" are dropped, not shown over the newer.
  const search = useAction(api.search, { latest: true });

  /** `limit` undefined: a new search, at the server's default page size. */
  const run = async (request: SearchRequest, limit?: number): Promise<boolean> => {
    if (!request.query) return false;
    const outcome = await search.run(limit === undefined ? request : { ...request, limit });
    if (!outcome.ok) return false;
    setResult(outcome.value);
    setAsked(request.query);
    setSubmitted(request);
    if (limit === undefined) setPageSize(outcome.value.limit);
    return true;
  };

  const sentHandoff = useRef(false);
  useEffect(() => {
    if (sentHandoff.current || (!handoff && !handoffState.collectionId)) return;
    sentHandoff.current = true;
    navigate(location.pathname, { replace: true, state: {} });
    if (handoff) void run(requestFor(handoff, filters));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // The button that asks for the weaker matches unmounts once they arrive,
  // which dropped keyboard focus to <body>. It moves to their heading instead.
  // Set only after the response is applied, so the heading it reaches is the
  // one for the matches just fetched.
  const weakerHeading = useRef<HTMLHeadingElement>(null);
  const [focusWeaker, setFocusWeaker] = useState(false);
  useEffect(() => {
    if (!focusWeaker) return;
    setFocusWeaker(false);
    weakerHeading.current?.focus();
  }, [focusWeaker]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void run(requestFor(query.trim(), filters));
  };

  // A filter applies at once to the search on screen - the results never
  // show one set of filters while the controls show another (a search still
  // running for older filters is superseded; see `search`).
  const changeFilters = (patch: Partial<Filters>) => {
    const next = { ...filters, ...patch };
    setFilters(next);
    if (submitted) void run(requestFor(submitted.query, next));
  };

  const collectionName = (id: string) =>
    collections.data?.results.find((item) => String(item.id) === id)?.name ?? "Collection";
  const activeFilters: { key: keyof Filters; label: string }[] = [
    ...(filters.document_type
      ? [{ key: "document_type" as const, label: `Type: ${TYPE_LABEL[filters.document_type as DocumentType] ?? filters.document_type}` }]
      : []),
    ...(filters.department ? [{ key: "department" as const, label: `Department: ${filters.department}` }] : []),
    ...(filters.collection ? [{ key: "collection" as const, label: `Collection: ${collectionName(filters.collection)}` }] : []),
  ];

  const limit = result?.limit ?? 0;
  const maxLimit = result?.max_limit ?? 0;
  // Passages the reranker left off the page as not relevant enough. Never
  // dropped for good: one click asks for them, marked and ordered last.
  const hidden = result?.ranking?.below_floor ?? 0;
  const showingWeaker = submitted?.include_below_floor ?? false;
  const strong = result?.results.filter((hit) => !hit.below_floor) ?? [];
  const weak = result?.results.filter((hit) => hit.below_floor) ?? [];
  const documents = new Set(strong.map((hit) => hit.document_id));
  const groups: ShownGroup[] = result ? (result.groups ?? groupsFromHits(strong)) : [];
  const floor = result?.ranking?.relevance_floor ?? null;
  // Every weaker match comes back, whatever the limit: the server caps the
  // matches at `limit` and appends all `hidden` passages below the floor.
  const showWeaker = async () => {
    if (submitted && (await run({ ...submitted, include_below_floor: true }, limit))) {
      setFocusWeaker(true);
    }
  };

  const notices = (result?.notices ?? [])
    .map((notice) =>
      describeNotice(notice, {
        clearFilters: () => changeFilters(NO_FILTERS),
        goTo: (path) => navigate(path),
        canOperate: can("view_operations"),
      }),
    )
    .filter((notice): notice is NoticeView => notice !== null);
  // The server's own words are for whoever runs it.
  const details = can("view_system_status") ? (result?.notes ?? []) : [];

  const askInstead = () =>
    navigate("/ask", {
      state: {
        question: asked,
        // The scope the results came from, so the answer does too.
        collectionId: submitted?.collection_id ?? undefined,
        documentType: submitted?.document_type || undefined,
        department: submitted?.department || undefined,
      },
    });

  return (
    <div className="page">
      <PageHeader
        title="Search"
        description="Finds passages by exact wording and by meaning, across every document you are allowed to read."
      />

      <form className="search-panel" onSubmit={submit} role="search">
        <div className="search-main">
          <label htmlFor="search-query" className="visually-hidden">
            Search documents
          </label>
          <div className="input-icon">
            <Icon name="search" size={18} />
            <input
              id="search-query"
              type="search"
              className="input input-lg"
              placeholder="Search for a rule, a figure, a reference number…"
              value={query}
              maxLength={1000}
              onChange={(event) => setQuery(event.target.value)}
              autoFocus
            />
          </div>
          <Button
            type="submit"
            variant="primary"
            size="lg"
            loading={search.pending}
            disabled={!query.trim()}
          >
            Search
          </Button>
        </div>
        <div className="search-filters">
          <span className="search-filters-label" aria-hidden="true">
            <Icon name="filter" size={14} />
            Filter
          </span>
          <select
            className="input input-sm"
            aria-label="Type"
            value={filters.document_type}
            onChange={(event) => changeFilters({ document_type: event.target.value })}
          >
            <option value="">Any type</option>
            {DOCUMENT_TYPES.map((value) => (
              <option key={value} value={value}>
                {TYPE_LABEL[value]}
              </option>
            ))}
          </select>
          {everyDepartment && (departments.data?.length ?? 0) > 1 ? (
            <select
              className="input input-sm"
              aria-label="Department"
              value={filters.department}
              onChange={(event) => changeFilters({ department: event.target.value })}
            >
              <option value="">Any department</option>
              {(departments.data ?? []).map((item) => (
                <option key={item.id} value={item.name}>
                  {item.name}
                </option>
              ))}
            </select>
          ) : null}
          {collections.data?.results.length ? (
            <select
              className="input input-sm"
              aria-label="Collection"
              value={filters.collection}
              onChange={(event) => changeFilters({ collection: event.target.value })}
            >
              <option value="">Any collection</option>
              {collections.data.results.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
          ) : null}
        </div>
        {activeFilters.length ? (
          <div className="filter-chips" role="group" aria-label="Active filters">
            {activeFilters.map((filter) => (
              <button
                key={filter.key}
                type="button"
                className="chip chip-sm chip-active"
                aria-label={`Remove filter ${filter.label}`}
                onClick={() => changeFilters({ [filter.key]: "" })}
              >
                {filter.label}
                <Icon name="x" size={12} />
              </button>
            ))}
            {activeFilters.length > 1 ? (
              <Button size="sm" variant="ghost" onClick={() => changeFilters(NO_FILTERS)}>
                Clear filters
              </Button>
            ) : null}
          </div>
        ) : null}
      </form>

      {search.error ? <ErrorState error={search.error} title="Search failed" /> : null}

      {search.pending && !result ? (
        <div className="card card-body">
          <Skeleton lines={6} />
        </div>
      ) : null}

      {result ? (
        <section
          aria-live="polite"
          aria-busy={search.pending}
          className={search.pending ? "search-results is-refreshing" : "search-results"}
        >
          <div className="result-summary">
            <p>
              <strong>{strong.length}</strong> passage{strong.length === 1 ? "" : "s"} from{" "}
              <strong>{documents.size}</strong> document{documents.size === 1 ? "" : "s"} for “{asked}”
              <span className="muted">
                {" "}
                · searched {result.retrieval.authorized_documents} you can access
                {typeof result.server_ms === "number" ? ` · ${(result.server_ms / 1000).toFixed(1)} s` : ""}
              </span>
            </p>
            <span className="badge-row">
              <Badge
                tone={result.semantic ? "info" : "muted"}
                icon={result.semantic ? "sparkles" : "hash"}
                title={
                  result.semantic
                    ? `Matched by meaning with ${result.embedding_model}`
                    : "Matched by wording only on this installation"
                }
              >
                {result.semantic ? "Keyword + meaning" : "Keyword matching"}
              </Badge>
              {result.ranking?.reranker && !result.ranking.degraded ? (
                <Badge tone="info" icon="layers" title={`Reordered by ${result.ranking.reranker}`}>
                  Ranked by relevance
                </Badge>
              ) : null}
            </span>
          </div>

          {notices.map((notice) => (
            <Alert
              key={notice.title}
              tone={notice.tone}
              title={notice.title}
              action={
                notice.action ? (
                  <Button size="sm" onClick={notice.action.onClick}>
                    {notice.action.label}
                  </Button>
                ) : undefined
              }
            >
              {notice.body}
            </Alert>
          ))}
          {details.length ? (
            <details className="search-details">
              <summary>Technical details</summary>
              <ul>
                {details.map((note) => (
                  <li key={note}>{note}</li>
                ))}
              </ul>
            </details>
          ) : null}

          {strong.length === 0 && hidden > 0 ? (
            <div className="card">
              <EmptyState title="No close match" icon="search">
                {plural(hidden, "passage")} share words or meaning with this search, but none is a
                close enough match.{" "}
                {weak.length ? `The closest ${weak.length === 1 ? "is" : plural(weak.length, "is", "are")} below. ` : null}
                {showingWeaker || weak.length >= hidden ? null : (
                  <Button variant="ghost" onClick={() => void showWeaker()} loading={search.pending}>
                    {weak.length ? `Show all ${hidden} weaker matches` : "Show the weaker matches"}
                  </Button>
                )}
              </EmptyState>
            </div>
          ) : strong.length === 0 ? (
            notices.length ? null : (
              <div className="card">
                <EmptyState title="Nothing found" icon="search">
                  No passage in the documents you can access matches this. Try other words, or
                  ask a question instead — the assistant can combine several passages.
                </EmptyState>
              </div>
            )
          ) : (
            <ol className="results" aria-label="Search results">
              {groups.map((group) => (
                <DocumentResult key={group.document_id} group={group} query={asked} floor={floor} />
              ))}
            </ol>
          )}

          {weak.length ? (
            <>
              <h2 className="results-weak-head" tabIndex={-1} ref={weakerHeading}>
                Weaker matches
              </h2>
              <p className="panel-note">
                <Icon name="info" size={14} />
                Scored below the relevance threshold, so they may only share words with the search.
              </p>
              <ol className="results results-weak" aria-label="Weaker matches">
                {weak.map((hit) => (
                  <WeakHit key={hit.chunk_id} hit={hit} query={asked} />
                ))}
              </ol>
            </>
          ) : null}

          <div className="button-row">
            {/* On the matches, not the whole response: weaker matches asked
                for are appended beyond `limit`, so the count alone said "there
                may be more" when every match was already shown. */}
            {strong.length >= limit && limit < maxLimit ? (
              <Button
                icon="chevron-down"
                onClick={() => submitted && void run(submitted, Math.min(maxLimit, limit + (pageSize || limit)))}
                loading={search.pending}
              >
                Show more results
              </Button>
            ) : null}
            {strong.length && hidden > 0 && !showingWeaker ? (
              <Button variant="ghost" onClick={() => void showWeaker()} loading={search.pending}>
                Show {plural(hidden, "weaker match", "weaker matches")}
              </Button>
            ) : null}
            {result.results.length || hidden ? (
              <Button variant="ghost" icon="sparkles" onClick={askInstead}>
                Ask this as a question instead
              </Button>
            ) : null}
          </div>
        </section>
      ) : !search.pending && !search.error ? (
        <div className="card">
          <EmptyState title="Search your documents" icon="search">
            Exact identifiers such as <code>REG/2024/117</code> work, and so do plain-language
            phrases. Results only ever come from documents you are allowed to read.
          </EmptyState>
        </div>
      ) : null}
    </div>
  );
}
