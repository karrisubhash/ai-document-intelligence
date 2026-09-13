import { api } from "../../api/client";
import { useResource } from "../../api/hooks";
import { useToken } from "../../auth/AuthContext";
import { Banner, ErrorBox, Field, JsonBlock, Pill, Spinner, formatTime } from "../ui";

function Confidence({ value }: { value: number }) {
  const percent = Math.max(0, Math.min(1, value)) * 100;

  return (
    <div className="meter" title={`${value.toFixed(4)}`}>
      <div className="meter-fill" style={{ width: `${percent}%` }} />
      <span className="meter-label">{(value * 100).toFixed(1)}%</span>
    </div>
  );
}

/**
 * Every classification verdict on the current version, newest first.
 *
 * An empty list is a real result, not a loading state: the stage records
 * SKIPPED when no classifier is configured, and saying so is the whole
 * point of never guessing a label.
 */
export default function ClassificationTab({
  documentId,
  refreshKey,
}: {
  documentId: number;
  refreshKey: string;
}) {
  const token = useToken();
  const results = useResource(
    () => api.classification(token, documentId),
    [token, documentId, refreshKey],
  );

  if (results.loading && !results.data) return <Spinner />;
  if (results.error) return <ErrorBox error={results.error} />;
  if (!results.data) return null;

  if (results.data.length === 0) {
    return (
      <Banner tone="warn" title="No classification verdict recorded">
        Either no classifier is configured (<code>DOCUMENT_CLASSIFIER</code> is empty,
        and the stage records <code>SKIPPED</code> rather than guessing a label), or
        the pipeline has not reached the stage. The Pipeline tab says which.
      </Banner>
    );
  }

  return (
    <div className="stack">
      {results.data.map((result, index) => (
        <div key={result.id} className="panel">
          <div className="panel-head">
            <div>
              <strong className="label-big">{result.predicted_label}</strong>
              {index === 0 ? <span className="muted small"> · latest</span> : null}
            </div>
            <Pill tone="info">
              {result.model_name} {result.model_version}
            </Pill>
          </div>

          <Confidence value={result.confidence} />

          <div className="fields">
            <Field label="Run at">{formatTime(result.created_at)}</Field>
            <Field label="From job">{result.processing_job ?? "—"}</Field>
            <Field label="Content">{result.document_content ?? "—"}</Field>
            <Field label="Input">
              {result.input_reference || <span className="muted">—</span>}
            </Field>
          </div>

          <JsonBlock value={result.metadata} label="scores / metadata" />
        </div>
      ))}
    </div>
  );
}
