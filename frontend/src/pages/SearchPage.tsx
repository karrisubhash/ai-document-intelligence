import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";

import { api } from "../api/client";
import { useAction, useResource } from "../api/hooks";
import { DOCUMENT_TYPES, type SearchHit, type SearchResponse } from "../api/types";
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
import { highlightTerms } from "../lib/highlight";
import { TYPE_LABEL } from "../lib/labels";

interface SearchRequest {
  query: string;
  document_type: string;
  department: string;
  collection_id: number | null;
  include_below_floor: boolean;
}

function Snippet({ text, query }: { text: string; query: string }) {
  const clipped = text.length > 420 ? `${text.slice(0, 420)}…` : text;
  return (
    <p className="snippet">
      {highlightTerms(clipped, query).map((segment, index) =>
        segment.highlighted ? <mark key={index}>{segment.text}</mark> : <span key={index}>{segment.text}</span>,
      )}
    </p>
  );
}

function Hit({ hit, query, semantic }: { hit: SearchHit; query: string; semantic: boolean }) {
  return (
    <li className="result">
      <div className="result-head">
        <Link
          className="result-title"
          to={`/documents/${hit.document_id}?tab=text&page=${hit.page_number}&start=${hit.character_start}&end=${hit.character_end}`}
        >
          {hit.document_title}
        </Link>
        <span className="muted small">Page {hit.page_number}</span>
        <span className="badge-row">
          {hit.matched_by.includes("lexical") ? <Badge tone="neutral">Wording</Badge> : null}
          {hit.matched_by.includes("vector") ? (
            <Badge tone="info">{semantic ? "Meaning" : "Similar wording"}</Badge>
          ) : null}
        </span>
      </div>
      <Snippet text={hit.text} query={query} />
    </li>
  );
}

const plural = (count: number, one: string, many = `${one}s`) => `${count} ${count === 1 ? one : many}`;

export default function SearchPage() {
  useDocumentTitle("Search");
  const navigate = useNavigate();
  const departments = useResource(() => api.departments(), []);
  const collections = useResource(() => api.collections(), []);

  // The query is deliberately NOT put in the URL. The search API takes it
  // by POST so that what people look for never lands in access logs or
  // browser history; mirroring it into the address bar would undo that.
  const [query, setQuery] = useState("");
  const [documentType, setDocumentType] = useState("");
  const [department, setDepartment] = useState("");
  const [collection, setCollection] = useState("");
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
  const search = useAction(api.search);

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
    void run({
      query: query.trim(),
      document_type: documentType,
      department,
      collection_id: collection ? Number(collection) : null,
      include_below_floor: false,
    });
  };

  const limit = result?.limit ?? 0;
  const maxLimit = result?.max_limit ?? 0;
  // Passages the reranker left off the page as not relevant enough. Never
  // dropped for good: one click asks for them, marked and ordered last.
  const hidden = result?.ranking?.below_floor ?? 0;
  const showingWeaker = submitted?.include_below_floor ?? false;
  const strong = result?.results.filter((hit) => !hit.below_floor) ?? [];
  const weak = result?.results.filter((hit) => hit.below_floor) ?? [];
  const documents = new Set(strong.map((hit) => hit.document_id));
  // Every weaker match comes back, whatever the limit: the server caps the
  // matches at `limit` and appends all `hidden` passages below the floor.
  const showWeaker = async () => {
    if (submitted && (await run({ ...submitted, include_below_floor: true }, limit))) {
      setFocusWeaker(true);
    }
  };

  return (
    <div className="page">
      <PageHeader
        title="Search"
        description="Finds passages by exact wording and by meaning, across every document you are allowed to read."
      />

      <form className="search-form" onSubmit={submit} role="search">
        <label htmlFor="search-query" className="visually-hidden">
          Search documents
        </label>
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
        <select
          className="input"
          aria-label="Type"
          value={documentType}
          onChange={(event) => setDocumentType(event.target.value)}
        >
          <option value="">Any type</option>
          {DOCUMENT_TYPES.map((value) => (
            <option key={value} value={value}>
              {TYPE_LABEL[value]}
            </option>
          ))}
        </select>
        <select
          className="input"
          aria-label="Department"
          value={department}
          onChange={(event) => setDepartment(event.target.value)}
        >
          <option value="">Any department</option>
          {(departments.data ?? []).map((item) => (
            <option key={item.id} value={item.name}>
              {item.name}
            </option>
          ))}
        </select>
        {collections.data?.results.length ? (
          <select
            className="input"
            aria-label="Collection"
            value={collection}
            onChange={(event) => setCollection(event.target.value)}
          >
            <option value="">Any collection</option>
            {collections.data.results.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </select>
        ) : null}
        <Button type="submit" variant="primary" loading={search.pending} disabled={!query.trim()}>
          Search
        </Button>
      </form>

      {search.error ? <ErrorState error={search.error} title="Search failed" /> : null}

      {search.pending && !result ? <Skeleton lines={6} /> : null}

      {result ? (
        <section aria-live="polite" aria-busy={search.pending}>
          <div className="result-summary">
            <p>
              <strong>{strong.length}</strong> passage{strong.length === 1 ? "" : "s"} from{" "}
              <strong>{documents.size}</strong> document{documents.size === 1 ? "" : "s"} for “{asked}”
              <span className="muted"> · searched {result.retrieval.authorized_documents} you can access</span>
            </p>
            <span className="badge-row">
              <Badge
                tone={result.semantic ? "info" : "muted"}
                title={
                  result.semantic
                    ? `Matched by meaning with ${result.embedding_model}`
                    : "Matched by wording only on this installation"
                }
              >
                {result.semantic ? "Keyword + meaning" : "Keyword matching"}
              </Badge>
              {result.ranking?.reranker && !result.ranking.degraded ? (
                <Badge tone="info" title={`Reordered by ${result.ranking.reranker}`}>
                  Ranked by relevance
                </Badge>
              ) : null}
            </span>
          </div>

          {result.notes.length ? (
            <Alert tone="info">
              {result.notes.map((note) => (
                <p key={note}>{note}</p>
              ))}
            </Alert>
          ) : null}

          {strong.length === 0 && hidden > 0 ? (
            <EmptyState title="No close match" icon="🔍">
              {plural(hidden, "passage")} share words or meaning with this search, but none is a
              close enough match.{" "}
              {weak.length ? `The closest ${weak.length === 1 ? "is" : plural(weak.length, "is", "are")} below. ` : null}
              {showingWeaker || weak.length >= hidden ? null : (
                <Button variant="ghost" onClick={() => void showWeaker()} loading={search.pending}>
                  {weak.length ? `Show all ${hidden} weaker matches` : "Show the weaker matches"}
                </Button>
              )}
            </EmptyState>
          ) : strong.length === 0 ? (
            <EmptyState title="Nothing found" icon="🔍">
              No passage in the documents you can access matches this. Try other words, or
              ask a question instead — the assistant can combine several passages.
            </EmptyState>
          ) : (
            <ol className="results" aria-label="Search results">
              {strong.map((hit) => (
                <Hit key={hit.chunk_id} hit={hit} query={asked} semantic={result.semantic} />
              ))}
            </ol>
          )}

          {weak.length ? (
            <>
              <h2 className="results-weak-head" tabIndex={-1} ref={weakerHeading}>
                Weaker matches
              </h2>
              <p className="muted small">
                Scored below the relevance threshold, so they may only share words with the search.
              </p>
              <ol className="results results-weak" aria-label="Weaker matches">
                {weak.map((hit) => (
                  <Hit key={hit.chunk_id} hit={hit} query={asked} semantic={result.semantic} />
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
              <Button variant="ghost" onClick={() => navigate("/ask", { state: { question: asked } })}>
                Ask this as a question instead
              </Button>
            ) : null}
          </div>
        </section>
      ) : !search.pending && !search.error ? (
        <EmptyState title="Search your documents" icon="⌕">
          Exact identifiers such as <code>REG/2024/117</code> work, and so do plain-language
          phrases. Results only ever come from documents you are allowed to read.
        </EmptyState>
      ) : null}
    </div>
  );
}
