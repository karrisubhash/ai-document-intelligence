import { api } from "../../api/client";
import { useResource } from "../../api/hooks";
import { useToken } from "../../auth/AuthContext";
import { Empty, ErrorBox, Excerpt, Field, JsonBlock, Pill, Spinner } from "../ui";

const METHOD_LABEL: Record<string, string> = {
  PDF_TEXT: "PDF text layer",
  OCR: "OCR",
  PLAIN_TEXT: "Plain text",
  DOCX_TEXT: "DOCX text",
};

/**
 * The normalized per-page text every downstream AI stage reads. Each page
 * carries the method that produced it, which is what makes the text-layer
 * path and the OCR path independently measurable.
 */
export default function ContentTab({
  documentId,
  refreshKey,
}: {
  documentId: number;
  refreshKey: string;
}) {
  const token = useToken();
  const content = useResource(
    () => api.content(token, documentId),
    [token, documentId, refreshKey],
  );

  if (content.loading && !content.data) return <Spinner />;

  if (content.status === 404) {
    return <Empty>No extracted content for this version yet.</Empty>;
  }

  if (content.error) return <ErrorBox error={content.error} />;
  if (!content.data) return null;

  const { data } = content;

  const methods = new Map<string, number>();
  for (const page of data.pages) {
    methods.set(page.extraction_method, (methods.get(page.extraction_method) ?? 0) + 1);
  }

  const characters = data.pages.reduce((total, page) => total + page.character_count, 0);

  return (
    <div className="stack">
      <div className="panel">
        <div className="fields">
          <Field label="Pages">{data.page_count}</Field>
          <Field label="Characters">{characters.toLocaleString()}</Field>
          <Field label="Pipeline version">{data.processing_version || "—"}</Field>
          <Field label="From job">{data.processing_job ?? "—"}</Field>
        </div>

        <div className="chips">
          {[...methods.entries()].map(([method, count]) => (
            <Pill key={method} tone="info">
              {METHOD_LABEL[method] ?? method}: {count} page(s)
            </Pill>
          ))}
        </div>

        <JsonBlock value={data.metadata} label="content metadata" />
      </div>

      {data.pages.length === 0 ? (
        <Empty>The content row exists but holds no pages.</Empty>
      ) : (
        data.pages.map((page) => (
          <div key={page.page_number} className="panel">
            <div className="panel-head">
              <strong>Page {page.page_number}</strong>
              <div className="panel-head-right">
                <Pill tone="muted">
                  {METHOD_LABEL[page.extraction_method] ?? page.extraction_method}
                </Pill>
                <span className="muted small">
                  {page.character_count.toLocaleString()} chars
                </span>
              </div>
            </div>
            <Excerpt text={page.text} />
            <JsonBlock value={page.metadata} label="page metadata" />
          </div>
        ))
      )}
    </div>
  );
}
