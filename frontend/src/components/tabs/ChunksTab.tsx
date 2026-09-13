import { useState } from "react";

import { api } from "../../api/client";
import { useResource } from "../../api/hooks";
import type { DocumentChunk } from "../../api/types";
import { useToken } from "../../auth/AuthContext";
import { Banner, Empty, ErrorBox, Excerpt, JsonBlock, Pill, Spinner } from "../ui";

function EmbeddingBadges({ chunk }: { chunk: DocumentChunk }) {
  if (chunk.embeddings.length === 0) {
    return (
      <Pill tone="warn" title="Lexically searchable, but not in the vector half">
        no embedding
      </Pill>
    );
  }

  return (
    <>
      {chunk.embeddings.map((embedding) => (
        <Pill
          key={embedding.id}
          tone={embedding.semantic ? "ok" : "warn"}
          title={
            embedding.semantic
              ? "Encodes meaning"
              : "Scores vocabulary overlap, not meaning"
          }
        >
          {embedding.model_name} {embedding.model_version} · {embedding.dimensions}d ·{" "}
          {embedding.semantic ? "semantic" : "lexical"}
        </Pill>
      ))}
    </>
  );
}

/**
 * The retrievable units search runs over. Page number and character span
 * travel with every chunk because they are most of a citation already -
 * and because a chunk never spans pages, exactly one page number is
 * always the right answer.
 */
export default function ChunksTab({
  documentId,
  refreshKey,
}: {
  documentId: number;
  refreshKey: string;
}) {
  const token = useToken();
  const [page, setPage] = useState(1);

  const chunks = useResource(
    () => api.chunks(token, documentId, page),
    [token, documentId, page, refreshKey],
  );

  if (chunks.loading && !chunks.data) return <Spinner />;

  if (chunks.status === 404) {
    return <Empty>No extracted content for this version yet, so no chunks.</Empty>;
  }

  if (chunks.error) return <ErrorBox error={chunks.error} />;
  if (!chunks.data) return null;

  if (chunks.data.results.length === 0) {
    return (
      <Banner tone="warn" title="No chunks indexed">
        The INDEXING stage writes chunks whenever content exists — the Pipeline tab
        says whether it ran.
      </Banner>
    );
  }

  const nonSemantic = chunks.data.results.some((chunk) =>
    chunk.embeddings.some((embedding) => !embedding.semantic),
  );

  return (
    <div className="stack">
      <div className="panel">
        <div className="panel-head">
          <strong>{chunks.data.count.toLocaleString()} chunk(s)</strong>
          <span className="muted small">
            {chunks.data.results[0].chunking_strategy} ·{" "}
            {chunks.data.results[0].chunking_version}
          </span>
        </div>

        {nonSemantic ? (
          <Banner tone="warn" title="Embeddings are lexical, not semantic">
            The bundled provider hashes word and character n-grams, so two texts are
            close when they share vocabulary — not meaning. Vector results here say
            nothing about whether the system understands the text.
          </Banner>
        ) : null}
      </div>

      {chunks.data.results.map((chunk) => (
        <div key={chunk.id} className="panel">
          <div className="panel-head">
            <div>
              <strong>chunk {chunk.chunk_index}</strong>
              <span className="muted small">
                {" "}
                · page {chunk.page_number} · chars {chunk.character_start}–
                {chunk.character_end} ({chunk.character_count})
              </span>
            </div>
            <div className="panel-head-right">
              <EmbeddingBadges chunk={chunk} />
            </div>
          </div>

          <Excerpt text={chunk.text} limit={400} />
          <JsonBlock value={chunk.metadata} />
        </div>
      ))}

      {chunks.data.next || chunks.data.previous ? (
        <div className="pager">
          <button
            className="button button-quiet"
            disabled={!chunks.data.previous}
            onClick={() => setPage((current) => current - 1)}
          >
            Previous
          </button>
          <span className="muted small">page {page}</span>
          <button
            className="button button-quiet"
            disabled={!chunks.data.next}
            onClick={() => setPage((current) => current + 1)}
          >
            Next
          </button>
        </div>
      ) : null}
    </div>
  );
}
