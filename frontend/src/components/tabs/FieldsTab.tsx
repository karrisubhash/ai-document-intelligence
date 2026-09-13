import { api } from "../../api/client";
import { useResource } from "../../api/hooks";
import type { ExtractedField } from "../../api/types";
import { useToken } from "../../auth/AuthContext";
import {
  Banner,
  ErrorBox,
  Field,
  JsonBlock,
  Pill,
  Spinner,
  formatTime,
  toneForFieldStatus,
} from "../ui";

function Span({ field }: { field: ExtractedField }) {
  if (field.page_number === null || field.character_start === null) {
    return <span className="muted">no span</span>;
  }

  return (
    <span className="mono small">
      p{field.page_number} · {field.character_start}–{field.character_end}
    </span>
  );
}

/**
 * One row per declared field, including the ones that were not found -
 * otherwise "looked and found nothing" is indistinguishable from "never
 * looked", and recall has no denominator.
 */
export default function FieldsTab({
  documentId,
  refreshKey,
}: {
  documentId: number;
  refreshKey: string;
}) {
  const token = useToken();
  const results = useResource(
    () => api.extraction(token, documentId),
    [token, documentId, refreshKey],
  );

  if (results.loading && !results.data) return <Spinner />;
  if (results.error) return <ErrorBox error={results.error} />;
  if (!results.data) return null;

  if (results.data.length === 0) {
    return (
      <Banner tone="warn" title="No extraction pass recorded">
        Either no extractor is configured (<code>DOCUMENT_FIELD_EXTRACTOR</code> is
        empty, and the stage records <code>SKIPPED</code> rather than inventing
        fields), no schema covers this document type, or the pipeline has not
        reached the stage. The Pipeline tab says which.
      </Banner>
    );
  }

  return (
    <div className="stack">
      {results.data.map((result, index) => (
        <div key={result.id} className="panel">
          <div className="panel-head">
            <div>
              <strong>
                {result.schema_name} {result.schema_version}
              </strong>
              {index === 0 ? <span className="muted small"> · latest</span> : null}
              <div className="muted small">
                {result.model_name} {result.model_version} · run against{" "}
                {result.document_type}
              </div>
            </div>
            <Pill tone={result.found_count > 0 ? "ok" : "warn"}>
              {result.found_count}/{result.field_count} found
            </Pill>
          </div>

          <div className="fields">
            <Field label="Run at">{formatTime(result.created_at)}</Field>
            <Field label="From job">{result.processing_job ?? "—"}</Field>
            <Field label="Content">{result.document_content ?? "—"}</Field>
          </div>

          <table className="table">
            <thead>
              <tr>
                <th>Field</th>
                <th>Status</th>
                <th>Value</th>
                <th>Confidence</th>
                <th>Span</th>
                <th>Matched text</th>
              </tr>
            </thead>
            <tbody>
              {result.extracted_fields.map((field) => (
                <tr key={field.id}>
                  <td className="mono">{field.field_name}</td>
                  <td>
                    <Pill tone={toneForFieldStatus(field.status)}>{field.status}</Pill>
                  </td>
                  <td>
                    {field.value || <span className="muted">—</span>}
                    <JsonBlock value={field.metadata} label="why" />
                  </td>
                  <td className="muted small">
                    {field.status === "FOUND" ? field.confidence.toFixed(2) : "—"}
                  </td>
                  <td>
                    <Span field={field} />
                  </td>
                  <td className="small">
                    {field.matched_text ? (
                      <code className="quote">{field.matched_text}</code>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <JsonBlock value={result.metadata} label="run metadata" />
        </div>
      ))}
    </div>
  );
}
