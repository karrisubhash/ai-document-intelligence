import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";

import { api } from "../api/client";
import { useAction, useResource } from "../api/hooks";
import { DOCUMENT_TYPES, type SearchResponse } from "../api/types";
import { Icon } from "../components/icons";
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

const PAGE = 20;

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

export default function SearchPage() {
  useDocumentTitle("Search");
  const navigate = useNavigate();
  const location = useLocation();
  const departments = useResource(() => api.departments(), []);
  const collections = useResource(() => api.collections(), []);

  // The query is deliberately NOT put in the URL. The search API takes it
  // by POST so that what people look for never lands in access logs or
  // browser history; mirroring it into the address bar would undo that.
  // Quick find hands a query over in history state instead, which this
  // page clears as soon as it has read it.
  const handoff = ((location.state as { query?: string } | null)?.query ?? "").trim();
  const [query, setQuery] = useState(handoff);
  const [documentType, setDocumentType] = useState("");
  const [department, setDepartment] = useState("");
  const [collection, setCollection] = useState("");
  const [limit, setLimit] = useState(PAGE);
  const [result, setResult] = useState<SearchResponse | null>(null);
  const [asked, setAsked] = useState("");
  // The search the results on screen came from. "Show more results" repeats
  // THAT one with a larger limit - it used to read the live inputs, so
  // editing the box or a filter without submitting replaced the list with a
  // different search.
  const [submitted, setSubmitted] = useState<{
    query: string;
    document_type: string;
    department: string;
    collection_id: number | null;
  } | null>(null);
  const search = useAction(api.search);

  const run = async (
    request: {
      query: string;
      document_type: string;
      department: string;
      collection_id: number | null;
    },
    nextLimit = PAGE,
  ) => {
    if (!request.query) return;
    setLimit(nextLimit);
    const outcome = await search.run({ ...request, limit: nextLimit });
    if (outcome.ok) {
      setResult(outcome.value);
      setAsked(request.query);
      setSubmitted(request);
    }
  };

  const sentHandoff = useRef(false);
  useEffect(() => {
    if (!handoff || sentHandoff.current) return;
    sentHandoff.current = true;
    navigate(location.pathname, { replace: true, state: {} });
    void run({ query: handoff, document_type: "", department: "", collection_id: null });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void run(
      {
        query: query.trim(),
        document_type: documentType,
        department,
        collection_id: collection ? Number(collection) : null,
      },
      PAGE,
    );
  };

  const documents = new Set(result?.results.map((hit) => hit.document_id));

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
            className="input input-sm"
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
              className="input input-sm"
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
        </div>
      </form>

      {search.error ? <ErrorState error={search.error} title="Search failed" /> : null}

      {search.pending && !result ? (
        <div className="card card-body">
          <Skeleton lines={6} />
        </div>
      ) : null}

      {result ? (
        <section aria-live="polite" aria-busy={search.pending}>
          <div className="result-summary">
            <p>
              <strong>{result.count}</strong> passage{result.count === 1 ? "" : "s"} from{" "}
              <strong>{documents.size}</strong> document{documents.size === 1 ? "" : "s"} for “
              {asked}”
              <span className="muted"> · searched {result.retrieval.authorized_documents} you can access</span>
            </p>
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
          </div>

          {result.notes.length ? (
            <Alert tone="info">
              {result.notes.map((note) => (
                <p key={note}>{note}</p>
              ))}
            </Alert>
          ) : null}

          {result.results.length === 0 ? (
            <div className="card">
              <EmptyState title="Nothing found" icon="search">
                No passage in the documents you can access matches this. Try other words, or
                ask a question instead — the assistant can combine several passages.
              </EmptyState>
            </div>
          ) : (
            <ol className="results">
              {result.results.map((hit) => (
                <li key={hit.chunk_id} className="result">
                  <span className="icon-tile" aria-hidden="true">
                    <Icon name="file-text" />
                  </span>
                  <div className="result-body">
                    <div className="result-head">
                      <Link
                        className="result-title"
                        to={`/documents/${hit.document_id}?tab=text&page=${hit.page_number}&start=${hit.character_start}&end=${hit.character_end}`}
                      >
                        {hit.document_title}
                      </Link>
                      <span className="result-page">
                        <Icon name="book-open" size={13} />
                        Page {hit.page_number}
                      </span>
                      <span className="badge-row">
                        {hit.matched_by.includes("lexical") ? <Badge tone="neutral">Wording</Badge> : null}
                        {hit.matched_by.includes("vector") ? (
                          <Badge tone="info">{result.semantic ? "Meaning" : "Similar wording"}</Badge>
                        ) : null}
                      </span>
                    </div>
                    <Snippet text={hit.text} query={asked} />
                  </div>
                </li>
              ))}
            </ol>
          )}

          <div className="button-row">
            {result.count >= limit && limit < 50 ? (
              <Button
                icon="chevron-down"
                onClick={() => submitted && void run(submitted, Math.min(50, limit + PAGE))}
                loading={search.pending}
              >
                Show more results
              </Button>
            ) : null}
            {result.results.length ? (
              <Button
                variant="ghost"
                icon="sparkles"
                onClick={() => navigate("/ask", { state: { question: asked } })}
              >
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
