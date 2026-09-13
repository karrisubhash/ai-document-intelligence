import type { ProcessingJob } from "../../api/types";
import {
  Empty,
  JsonBlock,
  Pill,
  duration,
  formatTime,
  toneForEventStatus,
} from "../ui";

function jobTone(status: string) {
  if (status === "COMPLETED") return "ok" as const;
  if (status === "FAILED") return "bad" as const;
  if (status === "PROCESSING" || status === "QUEUED") return "busy" as const;
  return "muted" as const;
}

/**
 * The stage-by-stage history of every processing job on this document.
 *
 * Jobs from earlier attempts are shown alongside the newest rather than
 * replaced: a reprocess creates a new job precisely so the old one's
 * output stays comparable, and hiding it here would undo that.
 */
export default function PipelineTab({ jobs }: { jobs: ProcessingJob[] }) {
  if (jobs.length === 0) {
    return <Empty>No processing job recorded for this document yet.</Empty>;
  }

  return (
    <div className="stack">
      {jobs.map((job, index) => (
        <div key={job.id} className="panel">
          <div className="panel-head">
            <div>
              <strong>Job #{job.id}</strong>{" "}
              {index === 0 ? <span className="muted small">· latest</span> : null}
              <div className="muted small">
                version {job.document_version} · attempt {job.attempt} ·{" "}
                {formatTime(job.created_at)}
              </div>
            </div>
            <div className="panel-head-right">
              {job.current_stage ? (
                <Pill tone="info">{job.current_stage}</Pill>
              ) : null}
              <Pill tone={jobTone(job.status)}>{job.status}</Pill>
            </div>
          </div>

          {job.error_code ? (
            <div className="banner banner-bad">
              <strong>{job.error_code}</strong>
              {job.error_message ? <div>{job.error_message}</div> : null}
            </div>
          ) : null}

          {job.events.length === 0 ? (
            <Empty>No stage events yet — the worker has not picked this up.</Empty>
          ) : (
            <ol className="timeline">
              {job.events.map((event) => (
                <li key={event.id} className={`timeline-item tl-${event.status}`}>
                  <div className="timeline-head">
                    <span className="stage">{event.stage}</span>
                    <Pill tone={toneForEventStatus(event.status)}>{event.status}</Pill>
                    <span className="muted small">
                      {duration(event.started_at, event.completed_at)}
                    </span>
                  </div>
                  {event.message ? (
                    <div className="timeline-message">{event.message}</div>
                  ) : null}
                  <JsonBlock value={event.metadata} />
                </li>
              ))}
            </ol>
          )}

          <div className="muted small">
            started {formatTime(job.started_at)} · finished{" "}
            {formatTime(job.completed_at)} · total{" "}
            {duration(job.started_at, job.completed_at)}
          </div>
        </div>
      ))}
    </div>
  );
}
