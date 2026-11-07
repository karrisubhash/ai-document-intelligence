import { Link } from "react-router-dom";

import type { DuplicateHint } from "../api/types";
import { Alert } from "./ui";
import "../styles/insights.css";

/*
 * The upload's exact-duplicate hint, shown after the fact: the file is
 * stored either way (a duplicate never blocks an upload), and the reader
 * decides whether to keep both. Only documents the uploader can read are
 * ever named - the API does not mention the rest, not even as a count.
 */

function describe(hint: DuplicateHint): string {
  const notes = [];
  if (!hint.is_current_version) notes.push("an earlier version");
  if (hint.is_archived) notes.push("archived");
  return notes.length ? ` (${notes.join(", ")})` : "";
}

/** For one file: a notice with a link to each document that already has it. */
export function DuplicateNotice({ duplicates }: { duplicates: DuplicateHint[] }) {
  return (
    <Alert tone="info" title="This file was already here">
      Uploaded. The same file, byte for byte, is already stored as:
      <ul className="duplicate-list">
        {duplicates.map((hint) => (
          <li key={hint.id}>
            <Link to={`/documents/${hint.id}`} className="link">
              {hint.title}
            </Link>
            {describe(hint)}
          </li>
        ))}
      </ul>
      Both are kept. Delete the new one if you do not need two copies.
    </Alert>
  );
}

/** For one row of a batch: "Same file as <title>". */
export function DuplicateNote({ duplicates }: { duplicates: DuplicateHint[] }) {
  const [first] = duplicates;
  return (
    <span className="upload-row-note">
      Same file as{" "}
      <Link to={`/documents/${first.id}`} className="link">
        {first.title}
      </Link>
      {duplicates.length > 1 ? ` and ${duplicates.length - 1} more` : ""}
    </span>
  );
}
