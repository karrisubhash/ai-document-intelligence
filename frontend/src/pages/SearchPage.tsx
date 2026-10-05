import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";

import { api } from "../api/client";
import { useAction, useResource } from "../api/hooks";
import { DOCUMENT_TYPES, type SearchResponse } from "../api/types";
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
  const departments = useResource(() => api.departments(), []);

  // The query is deliberately NOT put in the URL. The search API takes it
  // by POST so that what people look for never lands in access logs or
  // browser history; mirroring it into the address bar would undo that.
  const [query, setQuery] = useState("");
  const [documentType, setDocumentType] = useState("");
  const [department, setDepartment] = useState("");
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
  } | null>(null);
  const search = useAction(api.search);

  const run = async (
    request: { query: string; document_type: string; department: string },
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

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void run({ query: query.trim(), document_type: documentType, department }, PAGE);
  };

  const documents = new Set(result?.results.map((hit) => hit.document_id));

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
              <strong>{result.count}</strong> passage{result.count === 1 ? "" : "s"} from{" "}
              <strong>{documents.size}</strong> document{documents.size === 1 ? "" : "s"} for “
              {asked}”
              <span className="muted"> · searched {result.retrieval.authorized_documents} you can access</span>
            </p>
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
          </div>

          {result.notes.length ? (
            <Alert tone="info">
              {result.notes.map((note) => (
                <p key={note}>{note}</p>
              ))}
            </Alert>
          ) : null}

          {result.results.length === 0 ? (
            <EmptyState title="Nothing found" icon="🔍">
              No passage in the documents you can access matches this. Try other words, or
              ask a question instead — the assistant can combine several passages.
            </EmptyState>
          ) : (
            <ol className="results">
              {result.results.map((hit) => (
                <li key={hit.chunk_id} className="result">
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
                        <Badge tone="info">{result.semantic ? "Meaning" : "Similar wording"}</Badge>
                      ) : null}
                    </span>
                  </div>
                  <Snippet text={hit.text} query={asked} />
                </li>
              ))}
            </ol>
          )}

          <div className="button-row">
            {result.count >= limit && limit < 50 ? (
              <Button
                onClick={() => submitted && void run(submitted, Math.min(50, limit + PAGE))}
                loading={search.pending}
              >
                Show more results
              </Button>
            ) : null}
            {result.results.length ? (
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
