import { useEffect, useRef, useState, type DragEvent, type FormEvent } from "react";

import { ApiError, api } from "../api/client";
import { useResource } from "../api/hooks";
import {
  DOCUMENT_TYPES,
  type CreatedDocument,
  type DocumentDetail,
  type DocumentType,
} from "../api/types";
import { useAuth } from "../auth/AuthContext";
import {
  MAX_BATCH_FILES,
  addPlanned,
  planFiles,
  randomKey,
  uploadKey,
  type PickedFolder,
  type PlannedFile,
} from "../lib/batch";
import { BATCH_ACCEPT_ATTRIBUTE } from "../lib/files";
import { formatBytes } from "../lib/format";
import { TYPE_LABEL } from "../lib/labels";
import { ZipError } from "../lib/zip";
import { DuplicateNote, DuplicateNotice } from "./DuplicateNotice";
import { FileTypeIcon, Icon } from "./icons";
import { Alert, Badge, Button, Dialog, Field, IconButton, errorMessage, plural } from "./ui";

/**
 * Files uploading at once in a batch. Two keeps the connection busy while
 * one response is on its way back, without taking more than a share of the
 * server's request threads from everyone else.
 */
const CONCURRENCY = 2;

type Status =
  | { state: "waiting" }
  | { state: "uploading"; progress: number }
  | { state: "done"; document: DocumentDetail }
  | { state: "failed"; reason: string; retry: boolean };

type Phase = "choosing" | "uploading" | "finished";

function limitMessage(seconds: number | null): string {
  const wait =
    seconds === null
      ? "a little while"
      : seconds < 90
        ? "a minute"
        : `about ${Math.ceil(seconds / 60)} minutes`;
  return `You have reached the upload limit for now. Upload the rest in ${wait}.`;
}

/**
 * What was dropped. A dropped folder reaches `files` as an empty file with
 * no type, so it is told apart here - synchronously, as the items are only
 * readable during the drop event.
 */
function droppedItems(event: DragEvent): (File | PickedFolder)[] {
  const items = Array.from(event.dataTransfer.items ?? []).filter((item) => item.kind === "file");
  if (!items.length) return Array.from(event.dataTransfer.files ?? []);
  return items.flatMap((item): (File | PickedFolder)[] => {
    const entry = item.webkitGetAsEntry?.();
    if (entry?.isDirectory) return [{ folder: entry.name }];
    const file = item.getAsFile();
    return file ? [file] : [];
  });
}

type UploadDialogProps = {
  open: boolean;
  onClose: () => void;
  onUploaded: (document: DocumentDetail) => void;
  onBatchUploaded: (documents: DocumentDetail[]) => void;
};

/**
 * Upload, or - for a one-click demo guest, whose uploads the server refuses
 * - a word on why not and how to get an account. Every way in (a page's
 * button, quick find) opens this, so it is the one place to say it.
 */
export default function UploadDialog(props: UploadDialogProps) {
  const { can } = useAuth();
  if (can("is_guest")) {
    return (
      <Dialog
        open={props.open}
        onClose={props.onClose}
        title="Uploading is off in the demo"
        size="sm"
        footer={
          <Button variant="primary" onClick={props.onClose}>
            Got it
          </Button>
        }
      >
        <p className="guest-note">
          <Icon name="lock" size={16} />
          The demo workspace is read-only, so everyone who tries it finds the same sample
          library. Request an account to upload and ask about your own documents.
        </p>
      </Dialog>
    );
  }
  return <UploadForm {...props} />;
}

function UploadForm({
  open,
  onClose,
  onUploaded,
  onBatchUploaded,
}: {
  open: boolean;
  onClose: () => void;
  /** One file was chosen, and is now this document. */
  onUploaded: (document: DocumentDetail) => void;
  /** Several were: every document created, once the dialog is closed. */
  onBatchUploaded: (documents: DocumentDetail[]) => void;
}) {
  const { can, me } = useAuth();
  const anyDepartment = can("upload_to_any_department");
  const departments = useResource(() => api.departments(), [], {
    enabled: open && anyDepartment,
  });

  const [files, setFiles] = useState<PlannedFile[]>([]);
  const [statuses, setStatuses] = useState<Record<string, Status>>({});
  const [phase, setPhase] = useState<Phase>("choosing");
  const [opening, setOpening] = useState(false);
  const [documentType, setDocumentType] = useState<DocumentType>("GENERAL");
  const [department, setDepartment] = useState("");
  const [progress, setProgress] = useState<number | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [failure, setFailure] = useState<string | null>(null);
  const [halted, setHalted] = useState<string | null>(null);
  const [stopping, setStopping] = useState(false);
  const [dragging, setDragging] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const pickButton = useRef<HTMLButtonElement>(null);
  const list = useRef<HTMLUListElement>(null);
  // The list as last set, for a choice that finishes opening after other
  // changes were made: it is added to the list as it is then.
  const filesRef = useRef(files);
  filesRef.current = files;
  // Every change to the list goes through here, so the ref never trails it.
  const updateFiles = (next: PlannedFile[]) => {
    filesRef.current = next;
    setFiles(next);
  };
  // Choices are opened one after another, so a drop made while a zip is
  // still opening is added after it rather than lost.
  const adding = useRef<Promise<void>>(Promise.resolve());
  const pendingAdds = useRef(0);
  // Read by the upload workers between files, so a Stop, or a refusal that
  // every file would get, takes effect before the next one starts.
  const stopRef = useRef(false);
  const controller = useRef<AbortController | null>(null);
  // Each file's key for the settings it was last sent with (see uploadKey).
  const keys = useRef(new Map<string, { settings: string; key: string }>());
  const focusAfterRemove = useRef<number | null>(null);
  // A zip is opened asynchronously; what it held is dropped if the dialog
  // was closed or reopened meanwhile.
  const generation = useRef(0);
  // One file, uploaded, that the server says is already stored elsewhere:
  // the dialog stays open to say so, instead of moving straight on.
  const [sameFile, setSameFile] = useState<CreatedDocument | null>(null);

  useEffect(() => {
    if (!open) return;
    setSameFile(null);
    generation.current += 1;
    stopRef.current = false;
    controller.current = null;
    adding.current = Promise.resolve();
    pendingAdds.current = 0;
    keys.current = new Map();
    updateFiles([]);
    setStatuses({});
    setPhase("choosing");
    setOpening(false);
    setDocumentType("GENERAL");
    setDepartment("");
    setProgress(null);
    setErrors({});
    setFailure(null);
    setHalted(null);
    setStopping(false);
  }, [open]);

  // A removed row's button had the focus; it goes to the row that took its
  // place, or to the button that adds files, never to the page behind.
  useEffect(() => {
    const index = focusAfterRemove.current;
    if (index === null) return;
    focusAfterRemove.current = null;
    const buttons = list.current?.querySelectorAll<HTMLButtonElement>(".upload-row button");
    const target = buttons?.length ? buttons[Math.min(index, buttons.length - 1)] : pickButton.current;
    target?.focus();
  }, [files]);

  // One file chosen on its own keeps the form it always had: a title to
  // edit, and the document opens once it is uploaded.
  const single = files.length === 1 && !files[0].from;
  const uploadable = files.filter((file) => !file.skip);
  const skipped = files.length - uploadable.length;
  const tracked = uploadable.filter((file) => statuses[file.id]);
  const countOf = (state: Status["state"]) =>
    tracked.filter((file) => statuses[file.id].state === state).length;
  const done = countOf("done");
  const failed = countOf("failed");
  const remaining = uploadable.filter((file) => {
    const status = statuses[file.id];
    return !status || status.state === "waiting" || (status.state === "failed" && status.retry);
  });
  const created = uploadable.flatMap((file) => {
    const status = statuses[file.id];
    return status?.state === "done" ? [status.document] : [];
  });
  const uploadingSingle = progress !== null;
  const busy = phase === "uploading" || uploadingSingle;

  const close = () => {
    if (busy) return;
    if (sameFile) onBatchUploaded([sameFile]);
    else if (created.length) onBatchUploaded(created);
    else onClose();
  };

  const add = (picked: (File | PickedFolder)[]) => {
    if (!picked.length || phase !== "choosing" || uploadingSingle) return;
    const run = generation.current;
    pendingAdds.current += 1;
    setOpening(true);
    setErrors({});
    setFailure(null);
    adding.current = adding.current
      .then(async () => {
        const planned = await planFiles(picked);
        if (run !== generation.current) return;
        const next = addPlanned(filesRef.current, planned);
        if (typeof next === "string") {
          setErrors({ file: next });
        } else {
          updateFiles(next);
        }
      })
      .catch(() => setFailure("Those files could not be read."))
      .finally(() => {
        if (run === generation.current && --pendingAdds.current === 0) setOpening(false);
      });
  };

  const remove = (id: string) => {
    focusAfterRemove.current = filesRef.current.findIndex((file) => file.id === id);
    updateFiles(filesRef.current.filter((file) => file.id !== id));
    setErrors({});
  };

  const setStatus = (id: string, status: Status) =>
    setStatuses((current) => ({ ...current, [id]: status }));

  const keyFor = async (file: PlannedFile, title: string, fresh = false) => {
    const settings = JSON.stringify([title, documentType, anyDepartment ? department : ""]);
    const known = keys.current.get(file.id);
    if (!fresh && known?.settings === settings) return known.key;
    const key = fresh ? randomKey() : await uploadKey(file.identity, settings);
    keys.current.set(file.id, { settings, key });
    return key;
  };

  /**
   * One upload, under the file's key. The server answers a key it has seen
   * with the document it made then - unless that document has been deleted
   * since (404), or the key was its for a different request (422, a file
   * that only looked the same). Either way the file is new to it, and goes
   * once more under a fresh key.
   */
  const send = async (
    file: PlannedFile,
    title: string,
    content: File,
    onProgress: (fraction: number) => void,
    signal?: AbortSignal,
  ) => {
    const upload = {
      title,
      document_type: documentType,
      department: anyDepartment ? department : undefined,
      file: content,
    };
    try {
      return await api.upload(upload, onProgress, { idempotencyKey: await keyFor(file, title), signal });
    } catch (caught) {
      if (!(caught instanceof ApiError) || (caught.status !== 404 && caught.status !== 422)) throw caught;
      return api.upload(upload, onProgress, {
        idempotencyKey: await keyFor(file, title, true),
        signal,
      });
    }
  };

  const uploadOne = async (file: PlannedFile, signal: AbortSignal) => {
    setStatus(file.id, { state: "uploading", progress: 0 });
    let content: File;
    try {
      content = await file.load();
    } catch (caught) {
      const reason =
        caught instanceof ZipError
          ? caught.message
          : "It could not be read; it may have been moved or changed since it was chosen.";
      setStatus(file.id, { state: "failed", reason, retry: false });
      return;
    }
    try {
      const document = await send(
        file,
        file.title.trim() || file.name,
        content,
        (fraction) => setStatus(file.id, { state: "uploading", progress: fraction }),
        signal,
      );
      setStatus(file.id, { state: "done", document });
    } catch (caught) {
      if (caught instanceof DOMException && caught.name === "AbortError") {
        // Stopped. Sent again later under the same key, it cannot become
        // a second copy even if this attempt reached the server.
        setStatus(file.id, { state: "waiting" });
        return;
      }
      if (!(caught instanceof ApiError)) {
        setStatus(file.id, { state: "failed", reason: errorMessage(caught), retry: true });
        return;
      }
      if (caught.status === 429 || caught.status === 401) {
        // Every later file would be refused the same way. A 401 has
        // already sent the app back to sign-in.
        stopRef.current = true;
        setStatus(file.id, { state: "waiting" });
        if (caught.status === 429) setHalted(limitMessage(caught.retryAfter));
        return;
      }
      const fields = caught.fieldErrors();
      const everyFile = fields.department ?? fields.document_type;
      if (caught.status === 403 || everyFile) {
        // Refused for who is asking, or for a setting all the files share:
        // the rest would be refused the same way.
        stopRef.current = true;
        setHalted(everyFile ?? caught.message);
      }
      const reason =
        caught.status === 409
          ? "An earlier try is still in progress. Try again in a moment."
          : (fields.file ?? fields.title ?? everyFile ?? fields.non_field_errors ?? caught.message);
      // Refused for what it is (400, 413), it would be refused again. A
      // lost connection or a 5xx might not recur, nor a 403 that was a
      // session or CSRF cookie renewed since.
      const retry =
        caught.status === 0 || caught.status === 403 || caught.status === 409 || caught.status >= 500;
      setStatus(file.id, { state: "failed", reason, retry });
    }
  };

  const runBatch = async (queue: PlannedFile[]) => {
    const run = generation.current;
    const control = new AbortController();
    controller.current = control;
    stopRef.current = false;
    setStopping(false);
    setHalted(null);
    setFailure(null);
    setErrors({});
    for (const file of queue) setStatus(file.id, { state: "waiting" });
    setPhase("uploading");
    let next = 0;
    const worker = async () => {
      while (!stopRef.current && next < queue.length) await uploadOne(queue[next++], control.signal);
    };
    await Promise.all(Array.from({ length: Math.min(CONCURRENCY, queue.length) }, worker));
    if (run === generation.current) {
      controller.current = null;
      setPhase("finished");
    }
  };

  // Stops now: what is on its way is cancelled too, safely, as above.
  const stop = () => {
    stopRef.current = true;
    setStopping(true);
    controller.current?.abort();
  };

  const submitSingle = async (file: PlannedFile) => {
    const problems: Record<string, string> = {};
    if (file.skip) problems.file = file.skip;
    if (!file.title.trim()) problems.title = "Give the document a title.";
    setErrors(problems);
    setFailure(null);
    if (Object.keys(problems).length) return;

    setProgress(0);
    try {
      const created = await send(file, file.title.trim(), await file.load(), setProgress);
      if (created.possible_duplicates?.length) {
        setProgress(null);
        setSameFile(created);
        return;
      }
      onUploaded(created);
    } catch (caught) {
      setProgress(null);
      if (caught instanceof ApiError) {
        const fields = caught.fieldErrors();
        if (Object.keys(fields).length) {
          setErrors(fields);
          if (fields.non_field_errors) setFailure(fields.non_field_errors);
          return;
        }
        if (caught.status === 413) {
          setErrors({ file: caught.message });
          return;
        }
      }
      setFailure(errorMessage(caught));
    }
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (phase !== "choosing" || opening || uploadingSingle) return;
    if (!files.length) {
      setErrors({ file: "Choose a file to upload." });
      return;
    }
    if (single) {
      void submitSingle(files[0]);
      return;
    }
    if (!uploadable.length) {
      setErrors({ file: "None of these files can be uploaded." });
      return;
    }
    void runBatch(uploadable);
  };

  const onDrop = (event: DragEvent) => {
    event.preventDefault();
    setDragging(false);
    add(droppedItems(event));
  };

  const setTitle = (title: string) => {
    const [first, ...rest] = filesRef.current;
    updateFiles([{ ...first, title }, ...rest]);
  };

  const tracking = phase !== "choosing";
  const fileError = errors.file || (single ? files[0].skip : undefined);
  const overall = tracked.length
    ? tracked.reduce((sum, file) => {
        const status = statuses[file.id];
        if (status.state === "done" || status.state === "failed") return sum + 1;
        return status.state === "uploading" ? sum + status.progress : sum;
      }, 0) / tracked.length
    : 0;
  const shownProgress = uploadingSingle ? (progress ?? 0) : overall;

  // Before and during the upload; once it ends, the outcome is the alert's.
  let summary = "";
  if (!single && files.length && phase !== "finished") {
    summary =
      phase === "uploading"
        ? `${done} of ${tracked.length} uploaded` + (failed ? ` · ${failed} failed` : "")
        : `${uploadable.length} ${plural(uploadable.length, "file")} to upload`;
    if (skipped) summary += ` · ${skipped} skipped`;
  }

  let footer;
  if (sameFile) {
    footer = (
      <>
        <Button variant="ghost" onClick={close}>
          Close
        </Button>
        <Button variant="primary" onClick={() => onUploaded(sameFile)}>
          Open the new document
        </Button>
      </>
    );
  } else if (phase === "uploading") {
    footer = (
      <>
        <Button variant="ghost" onClick={stop} disabled={stopping}>
          {stopping ? "Stopping…" : "Stop"}
        </Button>
        <Button variant="primary" loading>
          Uploading…
        </Button>
      </>
    );
  } else if (phase === "finished") {
    footer = (
      <>
        {remaining.length ? (
          <Button icon="refresh" onClick={() => void runBatch(remaining)}>
            Upload {remaining.length} remaining
          </Button>
        ) : null}
        <Button variant="primary" onClick={close}>
          Done
        </Button>
      </>
    );
  } else {
    footer = (
      <>
        <Button variant="ghost" onClick={close} disabled={uploadingSingle}>
          Cancel
        </Button>
        <Button
          variant="primary"
          type="submit"
          form="upload-form"
          loading={uploadingSingle || opening}
        >
          {uploadingSingle
            ? `Uploading ${Math.round((progress ?? 0) * 100)}%`
            : single || !uploadable.length
              ? "Upload"
              : `Upload ${uploadable.length} ${plural(uploadable.length, "document")}`}
        </Button>
      </>
    );
  }

  return (
    <Dialog
      open={open}
      onClose={close}
      title="Upload documents"
      description="One file, several, or a .zip of them. Each is scanned, read and indexed in the background, then becomes searchable."
      footer={footer}
    >
      {sameFile?.possible_duplicates?.length ? (
        <DuplicateNotice duplicates={sameFile.possible_duplicates} />
      ) : null}
      <form
        id="upload-form"
        className="stack"
        onSubmit={submit}
        noValidate
        hidden={Boolean(sameFile)}
      >
        {!tracking ? (
          <div
            className={`dropzone${files.length ? " dropzone-compact" : ""}${dragging ? " dragging" : ""}${fileError ? " invalid" : ""}`}
            onDragOver={(event) => {
              event.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
          >
            <input
              ref={input}
              id="upload-file"
              type="file"
              multiple
              className="visually-hidden"
              accept={BATCH_ACCEPT_ATTRIBUTE}
              onChange={(event) => {
                const picked = Array.from(event.target.files ?? []);
                // Cleared, so choosing the same file again is a change.
                event.target.value = "";
                add(picked);
              }}
              aria-describedby="upload-file-hint"
            />
            {files.length ? (
              <>
                <p className="dropzone-title">Drop more here, or</p>
                <Button
                  ref={pickButton}
                  size="sm"
                  icon="plus"
                  onClick={() => input.current?.click()}
                  disabled={uploadingSingle}
                >
                  Add files
                </Button>
              </>
            ) : (
              <>
                <span className="dropzone-icon" aria-hidden="true">
                  <Icon name="upload-cloud" size={20} />
                </span>
                <p className="dropzone-title">Drop files or a .zip here</p>
                <Button
                  ref={pickButton}
                  size="sm"
                  icon="file"
                  onClick={() => input.current?.click()}
                >
                  Choose files
                </Button>
              </>
            )}
            <p className="field-hint" id="upload-file-hint">
              PDF, Word (.docx), plain text, or a PNG/JPEG scan, up to 50 MB each, or a .zip of
              them. Up to {MAX_BATCH_FILES} files at a time. Every upload is scanned before it is
              read.
            </p>
          </div>
        ) : null}
        {opening ? (
          <p className="muted small" role="status">
            Opening…
          </p>
        ) : null}

        {/* First, so the outcome is what a phone shows without scrolling. */}
        {phase === "finished" ? (
          <Alert
            tone={done === tracked.length ? "success" : done ? "warning" : "danger"}
            title={
              done === tracked.length
                ? `${done} ${plural(done, "document")} uploaded`
                : `${done} of ${tracked.length} uploaded`
            }
          >
            {[
              done === 1
                ? "It is read and indexed in the background, and becomes searchable when it is ready."
                : done
                  ? "They are read and indexed in the background; each becomes searchable when it is ready."
                  : null,
              halted,
            ]
              .filter(Boolean)
              .join(" ")}
          </Alert>
        ) : null}

        {files.length ? (
          <div className="upload-files">
            {summary ? (
              <p className="upload-summary" role="status">
                {summary}
              </p>
            ) : null}
            {/* Focusable, so the list scrolls from the keyboard when no row has a button. */}
            <ul ref={list} className="upload-list" aria-label="Chosen files" tabIndex={0}>
              {files.map((file) => (
                <UploadRow
                  key={file.id}
                  file={file}
                  status={statuses[file.id]}
                  phase={phase}
                  inline={!single}
                  onRemove={
                    phase === "choosing" && !uploadingSingle ? () => remove(file.id) : undefined
                  }
                />
              ))}
            </ul>
          </div>
        ) : null}
        {fileError ? <p className="field-error">{fileError}</p> : null}

        {single ? (
          <Field label="Title" error={errors.title} required>
            {(props) => (
              <input
                {...props}
                className="input"
                value={files[0].title}
                maxLength={255}
                disabled={uploadingSingle}
                onChange={(event) => setTitle(event.target.value)}
              />
            )}
          </Field>
        ) : null}

        {/* Settings for the whole batch; once it is under way they cannot change. */}
        {!tracking ? (
          <div className="form-row">
            <Field
              label="Type"
              error={errors.document_type}
              hint={files.length > 1 ? "For every file." : undefined}
            >
              {(props) => (
                <select
                  {...props}
                  className="input"
                  value={documentType}
                  disabled={busy}
                  onChange={(event) => setDocumentType(event.target.value as DocumentType)}
                >
                  {DOCUMENT_TYPES.map((value) => (
                    <option key={value} value={value}>
                      {TYPE_LABEL[value]}
                    </option>
                  ))}
                </select>
              )}
            </Field>

            {anyDepartment ? (
              <Field label="Department" error={errors.department} hint="Who may read it.">
                {(props) => (
                  <select
                    {...props}
                    className="input"
                    value={department}
                    disabled={busy}
                    onChange={(event) => setDepartment(event.target.value)}
                  >
                    <option value="">No department (only you)</option>
                    {(departments.data ?? []).map((item) => (
                      <option key={item.id} value={item.name}>
                        {item.name}
                      </option>
                    ))}
                  </select>
                )}
              </Field>
            ) : (
              <div className="field">
                <span className="field-label">Department</span>
                <p className="static-value">
                  {me?.department || "None — only you will be able to see it"}
                </p>
              </div>
            )}
          </div>
        ) : null}

        {busy ? (
          <div
            className="progress"
            role="progressbar"
            aria-label="Upload progress"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(shownProgress * 100)}
          >
            <span className="progress-fill" style={{ width: `${shownProgress * 100}%` }} />
          </div>
        ) : null}

        {failure ? <Alert tone="danger">{failure}</Alert> : null}
      </form>
    </Dialog>
  );
}

function UploadRow({
  file,
  status,
  phase,
  inline,
  onRemove,
}: {
  file: PlannedFile;
  status: Status | undefined;
  phase: Phase;
  /** Why a file is skipped, on its row. One file alone says it under the list. */
  inline: boolean;
  onRemove?: () => void;
}) {
  let badge = null;
  let note: string | undefined;
  if (file.skip) {
    badge = <Badge>Skipped</Badge>;
    if (inline) note = file.skip;
  } else if (status?.state === "uploading") {
    badge = <Badge tone="info">{Math.round(status.progress * 100)}%</Badge>;
  } else if (status?.state === "done") {
    badge = (
      <Badge tone="success" icon="check">
        Uploaded
      </Badge>
    );
  } else if (status?.state === "failed") {
    badge = <Badge tone="danger">Failed</Badge>;
    note = status.reason;
  } else if (status?.state === "waiting") {
    badge = <Badge>{phase === "finished" ? "Not uploaded" : "Waiting"}</Badge>;
  }

  return (
    <li className={`upload-row${file.skip ? " upload-row-skipped" : ""}`}>
      <FileTypeIcon fileName={file.name} size="sm" />
      <span className="upload-row-text">
        <span className="upload-row-name">{file.name}</span>
        <span className="muted small">
          {file.size > 0 ? formatBytes(file.size) : null}
          {file.size > 0 && file.from ? " · " : null}
          {file.from ? `in ${file.from}` : null}
        </span>
        {note ? <span className="upload-row-note">{note}</span> : null}
        {status?.state === "done" && (status.document as CreatedDocument).possible_duplicates?.length ? (
          <DuplicateNote duplicates={(status.document as CreatedDocument).possible_duplicates!} />
        ) : null}
      </span>
      {badge}
      {onRemove ? (
        <IconButton icon="x" size="sm" label={`Remove ${file.name}`} onClick={onRemove} />
      ) : null}
    </li>
  );
}
