import { api } from "../../api/client";
import { useResource } from "../../api/hooks";
import { Icon } from "../../components/icons";
import {
  Alert,
  Badge,
  Button,
  Card,
  DescriptionList,
  ErrorState,
  PageHeader,
  Skeleton,
  useDocumentTitle,
} from "../../components/ui";
import { formatDateTime } from "../../lib/format";
import { humanize } from "../../lib/labels";

export default function SystemPage() {
  useDocumentTitle("System status");
  const status = useResource(() => api.adminSystem(), []);
  const data = status.data;

  return (
    <div className="page">
      <PageHeader
        title="System status"
        description="Dependencies, workload and which AI components are configured. No secrets are shown here."
        actions={
          <Button icon="refresh" onClick={status.reload} loading={status.loading}>
            Check again
          </Button>
        }
      />
      {status.error && !data ? (
        <ErrorState error={status.error} onRetry={status.reload} />
      ) : !data ? (
        <div className="card card-body">
          <Skeleton lines={8} />
        </div>
      ) : (
        <div className="stack">
          {data.debug ? (
            <Alert tone="warning" title="Debug mode is on">
              This installation is running with DJANGO_DEBUG enabled. That is for development
              only and must be off in production.
            </Alert>
          ) : null}
          <p className="checked-at">
            <Icon name="clock" size={14} />
            Checked {formatDateTime(data.checked_at)}
          </p>

          <div className="grid-4">
            {Object.entries(data.components).map(([name, component]) => (
              <div key={name} className={`health health-${component.status}`}>
                <div className="health-head">
                  <span className="health-name">{humanize(name)}</span>
                  <Badge
                    tone={component.status === "ok" ? "success" : component.status === "skipped" ? "muted" : "danger"}
                    dot
                  >
                    {component.status === "ok" ? "Healthy" : component.status === "skipped" ? "Not checked" : "Failing"}
                  </Badge>
                </div>
                <span className="health-detail">
                  {component.error
                    ? component.error
                    : component.status === "skipped"
                      ? "Not checked on this installation"
                      : `${component.latency_ms} ms`}
                </span>
              </div>
            ))}
          </div>

          <div className="grid-2">
            <Card title="Workload" icon="activity">
              <DescriptionList
                items={[
                  ...Object.entries(data.queues).map(
                    ([queue, depth]) => [`Queue “${queue}”`, `${depth} waiting`] as [string, string],
                  ),
                  ["Stalled runs now", String(data.jobs_last_24h.stalled_now)],
                  ["Timeouts (24 h)", String(data.jobs_last_24h.timeouts)],
                  ...Object.entries(data.jobs_last_24h.by_status).map(
                    ([name, count]) => [`${humanize(name)} runs (24 h)`, String(count)] as [string, string],
                  ),
                ]}
              />
            </Card>
            <Card title="Content" icon="database">
              <DescriptionList
                items={[
                  ...Object.entries(data.documents.by_status).map(
                    ([name, count]) => [`${humanize(name)} documents`, String(count)] as [string, string],
                  ),
                  ["Archived documents", String(data.documents.archived)],
                  ["Active accounts", String(data.users.active)],
                  ["Inactive accounts", String(data.users.inactive)],
                ]}
              />
            </Card>
          </div>

          <Card title="AI configuration" icon="cpu">
            <DescriptionList
              items={[
                [
                  "Embeddings",
                  data.ai.embedding.provider ? (
                    <>
                      {data.ai.embedding.provider} {data.ai.embedding.version}{" "}
                      <Badge tone={data.ai.embedding.semantic ? "info" : "muted"}>
                        {data.ai.embedding.semantic ? "semantic" : "lexical only"}
                      </Badge>
                    </>
                  ) : (
                    "None — keyword search only"
                  ),
                ],
                [
                  "Reranking",
                  <>
                    {data.ai.reranker.name} {data.ai.reranker.version}{" "}
                    <Badge tone={data.ai.reranker.semantic ? "info" : "muted"}>
                      {data.ai.reranker.semantic ? "cross-encoder" : "lexical"}
                    </Badge>
                  </>,
                ],
                [
                  "Answering",
                  <>
                    {data.ai.answerer.name} {data.ai.answerer.version}{" "}
                    <Badge tone={data.ai.answerer.generative ? "info" : "muted"}>
                      {data.ai.answerer.generative ? "generative" : "extractive (quotes only)"}
                    </Badge>
                  </>,
                ],
                [
                  "Chunking",
                  `${data.ai.chunking.strategy} ${data.ai.chunking.version}, ${data.ai.chunking.size} chars / ${data.ai.chunking.overlap} overlap`,
                ],
                [
                  "Relevance floor",
                  String(data.ai.retrieval.relevance_floor ?? "—"),
                ],
                ["Classifier", data.pipeline.classifier ? "Configured" : "Not configured"],
                ["Field extractor", data.pipeline.field_extractor ? "Configured" : "Not configured"],
                ["Malware scanner", data.pipeline.scanner ? "Configured" : "Not configured — uploads are not scanned"],
                ["Object storage", data.pipeline.object_storage ? "S3-compatible bucket" : "Local disk"],
              ]}
            />
          </Card>
        </div>
      )}
    </div>
  );
}
