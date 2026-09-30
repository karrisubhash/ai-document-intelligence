import { useEffect, useRef, useState, type DragEvent, type FormEvent } from "react";

import { ApiError, api } from "../api/client";
import { useResource } from "../api/hooks";
import { DOCUMENT_TYPES, type DocumentDetail, type DocumentType } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { formatBytes } from "../lib/format";
import { ACCEPT_ATTRIBUTE, titleFromFilename, validateFile } from "../lib/files";
import { TYPE_LABEL } from "../lib/labels";
import { Alert, Button, Dialog, Field, errorMessage } from "./ui";

export default function UploadDialog({
  open,
  onClose,
  onUploaded,
}: {
  open: boolean;
  onClose: () => void;
  onUploaded: (document: DocumentDetail) => void;
}) {
  const { can, me } = useAuth();
  const anyDepartment = can("upload_to_any_department");
  const departments = useResource(() => api.departments(), [], {
    enabled: open && anyDepartment,
  });

  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [titleTouched, setTitleTouched] = useState(false);
  const [documentType, setDocumentType] = useState<DocumentType>("GENERAL");
  const [department, setDepartment] = useState("");
  const [progress, setProgress] = useState<number | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [failure, setFailure] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    setFile(null);
    setTitle("");
    setTitleTouched(false);
    setDocumentType("GENERAL");
    setDepartment("");
    setProgress(null);
    setErrors({});
    setFailure(null);
  }, [open]);

  const choose = (chosen: File | null) => {
    setFile(chosen);
    const problem = validateFile(chosen);
    setErrors((current) => ({ ...current, file: problem ?? "" }));
    if (chosen && !titleTouched) setTitle(titleFromFilename(chosen.name));
  };

  const onDrop = (event: DragEvent) => {
    event.preventDefault();
    setDragging(false);
    choose(event.dataTransfer.files?.[0] ?? null);
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const problems: Record<string, string> = {};
    const fileProblem = validateFile(file);
    if (fileProblem) problems.file = fileProblem;
    if (!title.trim()) problems.title = "Give the document a title.";
    setErrors(problems);
    setFailure(null);
    if (Object.keys(problems).length || !file) return;

    setProgress(0);
    try {
      const created = await api.upload(
        {
          title: title.trim(),
          document_type: documentType,
          department: anyDepartment ? department : undefined,
          file,
        },
        setProgress,
      );
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

  const uploading = progress !== null;

  return (
    <Dialog
      open={open}
      onClose={uploading ? () => undefined : onClose}
      title="Upload a document"
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={uploading}>
            Cancel
          </Button>
          <Button variant="primary" type="submit" form="upload-form" loading={uploading}>
            {uploading ? `Uploading ${Math.round((progress ?? 0) * 100)}%` : "Upload"}
          </Button>
        </>
      }
    >
      <form id="upload-form" className="stack" onSubmit={submit} noValidate>
        <div
          className={`dropzone${dragging ? " dragging" : ""}${errors.file ? " invalid" : ""}`}
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
            className="visually-hidden"
            accept={ACCEPT_ATTRIBUTE}
            onChange={(event) => choose(event.target.files?.[0] ?? null)}
            aria-describedby="upload-file-hint"
          />
          {file ? (
            <div className="dropzone-file">
              <strong>{file.name}</strong>
              <span className="muted">{formatBytes(file.size)}</span>
              <Button size="sm" variant="ghost" onClick={() => input.current?.click()}>
                Choose another
              </Button>
            </div>
          ) : (
            <>
              <p className="dropzone-title">Drop a file here</p>
              <Button size="sm" onClick={() => input.current?.click()}>
                Choose a file
              </Button>
            </>
          )}
          <p className="field-hint" id="upload-file-hint">
            PDF, Word (.docx), plain text, or a PNG/JPEG scan. Up to 50 MB. Every upload is
            scanned before it is read.
          </p>
        </div>
        {errors.file ? <p className="field-error">{errors.file}</p> : null}

        <Field label="Title" error={errors.title} required>
          {(props) => (
            <input
              {...props}
              className="input"
              value={title}
              maxLength={255}
              onChange={(event) => {
                setTitle(event.target.value);
                setTitleTouched(true);
              }}
            />
          )}
        </Field>

        <div className="form-row">
          <Field label="Type" error={errors.document_type}>
            {(props) => (
              <select
                {...props}
                className="input"
                value={documentType}
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

        {uploading ? (
          <div
            className="progress"
            role="progressbar"
            aria-label="Upload progress"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round((progress ?? 0) * 100)}
          >
            <span className="progress-fill" style={{ width: `${(progress ?? 0) * 100}%` }} />
          </div>
        ) : null}

        {failure ? <Alert tone="danger">{failure}</Alert> : null}
      </form>
    </Dialog>
  );
}
