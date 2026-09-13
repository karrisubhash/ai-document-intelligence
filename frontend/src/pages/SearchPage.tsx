import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import { DOCUMENT_TYPES } from "../api/types";
import type { SearchResponse } from "../api/types";
import { useToken } from "../auth/AuthContext";
import { Banner, Empty, ErrorBox, Excerpt, Field, Pill } from "../components/ui";

export default function SearchPage() {
  const token = useToken();

  const [query, setQuery] = useState("");
  const [limit, setLimit] = useState(10);
  const [documentType, setDocumentType] = useState("");
  const [department, setDepartment] = useState("");

  const [response, setResponse] = useState<SearchResponse | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);

    try {
      setResponse(
        await api.search(token, {
          query,
          limit,
          document_type: documentType,
          department,
        }),
      );
    } catch (caught) {
      setError(caught);
      setResponse(null);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack">
      <form className="card" onSubmit={submit}>
        <h2>Hybrid search</h2>
        <p className="muted">
          Lexical and vector retrieval, fused by reciprocal rank. Authorization runs
          before similarity — an unauthorized chunk is never a candidate, so it cannot
          influence scores or counts.
        </p>

        <div className="row">
          <label className="grow">
            <span>Query</span>
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="attendance requirement, or REG/2024/117"
              required
            />
          </label>

          <label>
            <span>Limit</span>
            <input
              type="number"
              min={1}
              max={50}
              value={limit}
              onChange={(event) => setLimit(Number(event.target.value))}
            />
          </label>

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

        <button className="button" type="submit" disabled={busy || !query.trim()}>
          {busy ? "Searching…" : "Search"}
        </button>
      </form>

      {response ? (
        <div className="card">
          <div className="card-head">
            <h2>{response.count} result(s)</h2>
            <div className="card-head-right">
              <Pill tone={response.semantic ? "ok" : "warn"}>
                {response.semantic ? "semantic vectors" : "lexical vectors"}
              </Pill>
              <Pill tone="muted">{response.embedding_model || "no embedding model"}</Pill>
            </div>
          </div>

          {!response.semantic ? (
            <Banner tone="warn" title="The vector half is not semantic">
              The configured embedding provider scores vocabulary overlap, not meaning.
              A miss here is not evidence that the system failed to understand the
              query — it never claimed to.
            </Banner>
          ) : null}

          <div className="fields">
            <Field label="Lexical candidates">
              {response.retrieval.lexical_candidates}
            </Field>
            <Field label="Vector candidates">
              {response.retrieval.vector_candidates}
            </Field>
            <Field label="Authorized documents">
              {response.retrieval.authorized_documents}
            </Field>
          </div>

          {response.notes.length > 0 ? (
            <ul className="notes">
              {response.notes.map((note) => (
                <li key={note}>{note}</li>
              ))}
            </ul>
          ) : null}

          {response.results.length === 0 ? (
            <Empty>
              Nothing matched. With {response.retrieval.authorized_documents} document(s)
              in scope, that is a retrieval result, not an authorization error.
            </Empty>
          ) : (
            <ol className="hits">
              {response.results.map((hit, index) => (
                <li key={hit.chunk_id} className="panel">
                  <div className="panel-head">
                    <div>
                      <span className="rank">#{index + 1}</span>{" "}
                      <Link to={`/documents/${hit.document_id}`}>
                        {hit.document_title}
                      </Link>
                      <span className="muted small">
                        {" "}
                        · page {hit.page_number} · chars {hit.character_start}–
                        {hit.character_end} · chunk {hit.chunk_id}
                      </span>
                    </div>
                    <div className="panel-head-right">
                      {hit.matched_by.map((how) => (
                        <Pill key={how} tone={how === "vector" ? "info" : "muted"}>
                          {how}
                          {how === "lexical" && hit.lexical_rank !== null
                            ? ` #${hit.lexical_rank}`
                            : ""}
                          {how === "vector" && hit.vector_rank !== null
                            ? ` #${hit.vector_rank}`
                            : ""}
                        </Pill>
                      ))}
                      <Pill tone="ok" title="Reciprocal-rank fusion score">
                        {hit.score.toFixed(4)}
                      </Pill>
                    </div>
                  </div>

                  <Excerpt text={hit.text} limit={500} />
                </li>
              ))}
            </ol>
          )}
        </div>
      ) : null}
    </div>
  );
}
