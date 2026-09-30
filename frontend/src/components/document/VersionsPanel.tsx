import { useRef, useState } from "react";

import { ApiError, api, saveBlob } from "../../api/client";
import { useResource } from "../../api/hooks";
import type { DocumentDetail } from "../../api/types";
import { formatBytes, formatDateTime } from "../../lib/format";
import { ACCEPT_ATTRIBUTE, validateFile } from "../../lib/files";
import { Alert, Badge, Button, ErrorState, Skeleton, errorMessage, useToast } from "../ui";

export default function VersionsPanel({
  document,
  onChanged,
}: {
  document: DocumentDetail;
  onChanged: () => void;
}) {
  const toast = useToast();
  const versions = useResource(() => api.versions(document.id), [document.id]);
  const input = useRef<HTMLInputElement>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [downloading, setDownloading] = useState<number | null>(null);

  const upload = async (file: File | null) => {
    const invalid = validateFile(file);
    if (invalid || !file) {
      setProblem(invalid);
      return;
    }
    setProblem(null);
    setProgress(0);
    try {
      const version = await api.uploadVersion(document.id, file, setProgress);
      toast(`Version ${version.version_number} uploaded; processing has started.`);
      versions.reload();
      onChanged();
    } catch (caught) {
      setProblem(
        caught instanceof ApiError && caught.status === 409
          ? "This document is archived. Restore it before adding a version."
          : errorMessage(caught),
      );
    } finally {
      setProgress(null);
      if (input.current) input.current.value = "";
    }
  };

  const download = async (versionNumber: number, name: string) => {
    setDownloading(versionNumber);
    try {
      saveBlob(await api.fileBlob(document.id, versionNumber), name);
    } catch (caught) {
      toast(errorMessage(caught), "danger");
    } finally {
      setDownloading(null);
    }
  };

  if (versions.loading && !versions.data) return <Skeleton lines={4} />;
  if (versions.error) return <ErrorState error={versions.error} onRetry={versions.reload} />;

  return (
    <div className="stack">
      <div className="panel-intro">
        <p className="muted small">
          Every upload is kept as its own immutable version. Search and answers use the
          current version only; earlier ones stay available as a record.
        </p>
        {document.can_manage && !document.is_archived ? (
          <>
            <input
              ref={input}
              type="file"
              className="visually-hidden"
              accept={ACCEPT_ATTRIBUTE}
              aria-label="Choose a file for the new version"
              onChange={(event) => upload(event.target.files?.[0] ?? null)}
            />
            <Button
              onClick={() => input.current?.click()}
              loading={progress !== null}
            >
              {progress !== null
                ? `Uploading ${Math.round(progress * 100)}%`
                : "Upload new version"}
            </Button>
          </>
        ) : null}
      </div>
      {problem ? <Alert tone="danger">{problem}</Alert> : null}
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Version</th>
              <th scope="col">File</th>
              <th scope="col">Size</th>
              <th scope="col">Uploaded</th>
              <th scope="col">SHA-256</th>
              <th scope="col">
                <span className="visually-hidden">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {(versions.data ?? []).map((version) => (
              <tr key={version.id}>
                <td>
                  v{version.version_number}{" "}
                  {version.id === document.current_version?.id ? (
                    <Badge tone="info">Current</Badge>
                  ) : null}
                </td>
                <td>{version.file_name}</td>
                <td>{formatBytes(version.file_size)}</td>
                <td>{formatDateTime(version.created_at)}</td>
                <td>
                  <code className="checksum" title={version.checksum}>
                    {version.checksum?.slice(0, 12)}…
                  </code>
                </td>
                <td>
                  <Button
                    size="sm"
                    loading={downloading === version.version_number}
                    onClick={() => download(version.version_number, version.file_name)}
                  >
                    Download
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
