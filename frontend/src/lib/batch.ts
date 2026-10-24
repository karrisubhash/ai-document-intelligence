/**
 * What someone picked or dropped - files, zips of files, or both - as the
 * list the upload dialog works through: one entry per document it would
 * create, and one per file it will not upload, with the reason.
 *
 * Every entry becomes its own upload through the ordinary endpoint, so the
 * server checks each one exactly as it checks a single file; nothing here
 * is a control, only an early answer (see lib/files.ts).
 */
import {
  MAX_UPLOAD_BYTES,
  MIME_BY_EXTENSION,
  extensionOf,
  fileProblem,
  titleFromFilename,
} from "./files";
import { ZipError, canRead, listZip, readZipEntry, type ZipEntry } from "./zip";

/** How many documents one upload may create - as many as one bulk delete removes. */
export const MAX_BATCH_FILES = 100;

export interface PlannedFile {
  id: string;
  /** The file name the server receives. */
  name: string;
  title: string;
  size: number;
  /** For a file from a zip: the archive, and the folder inside it. */
  from?: string;
  /** Why it will not be uploaded. Absent for a file that will be. */
  skip?: string;
  /** Sent as Idempotency-Key, so a retry can never create a second copy. */
  key: string;
  load: () => Promise<File>;
}

export function isZip(name: string): boolean {
  return extensionOf(name) === ".zip";
}

/**
 * What an operating system adds to an archive unasked: Finder's resource
 * forks and folder settings, Explorer's thumbnail caches. Nobody chose
 * them, so they are left out rather than listed as skipped.
 */
export function isSystemFile(path: string): boolean {
  const parts = path.split("/").filter(Boolean);
  const base = parts[parts.length - 1] ?? "";
  return (
    parts.includes("__MACOSX") ||
    base.startsWith("._") ||
    [".DS_Store", "Thumbs.db", "desktop.ini"].includes(base)
  );
}

/** 128 random bits as hex. crypto.randomUUID needs a secure context; this does not. */
export function randomKey(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function planned(name: string, size: number, load: () => Promise<File>, from?: string): PlannedFile {
  return {
    id: randomKey(),
    name,
    // The server's limit; a long file name is not a reason to refuse.
    title: titleFromFilename(name).slice(0, 255),
    size,
    from,
    skip: fileProblem(name, size) ?? undefined,
    key: randomKey(),
    load,
  };
}

function fromEntry(archive: File, entry: ZipEntry): PlannedFile {
  const folder = entry.path.slice(0, entry.path.length - entry.name.length).replace(/\/$/, "");
  const from = folder ? `${archive.name} › ${folder}` : archive.name;
  const item = planned(
    entry.name,
    entry.size,
    async () => {
      const blob = await readZipEntry(archive, entry, MAX_UPLOAD_BYTES);
      return new File([blob], entry.name, {
        type: MIME_BY_EXTENSION[extensionOf(entry.name)] ?? "",
      });
    },
    from,
  );
  if (entry.link) item.skip = "It is a link, not a file.";
  else if (isZip(entry.name)) item.skip = "A zip inside a zip is not opened. Upload it on its own.";
  else if (entry.encrypted) item.skip = "It is password-protected.";
  else if (!item.skip && !canRead(entry)) {
    item.skip = "It is compressed in a way the browser cannot open.";
  }
  return item;
}

async function openZip(archive: File): Promise<PlannedFile[]> {
  let entries: ZipEntry[];
  try {
    entries = await listZip(archive);
  } catch (caught) {
    const item = planned(archive.name, archive.size, () => Promise.reject(caught));
    item.skip = caught instanceof ZipError ? caught.message : "This zip could not be opened.";
    return [item];
  }
  const files = entries
    .filter((entry) => !entry.directory && !isSystemFile(entry.path))
    .map((entry) => fromEntry(archive, entry));
  if (!files.length) {
    const item = planned(archive.name, archive.size, () => Promise.reject(new ZipError("Empty")));
    item.skip = "This zip has no files in it.";
    return [item];
  }
  return files;
}

/** A dropped folder, which the browser hands over as an empty, typeless file. */
export interface PickedFolder {
  folder: string;
}

/**
 * Plans `picked` after the `already` planned, in the order chosen, a zip's
 * files in its own order. Past MAX_BATCH_FILES uploadable files, the rest
 * are listed as skipped, so nobody is left wondering where they went.
 */
export async function planFiles(
  picked: (File | PickedFolder)[],
  already: PlannedFile[] = [],
): Promise<PlannedFile[]> {
  const added: PlannedFile[] = [];
  for (const file of picked) {
    if (!(file instanceof File)) {
      const item = planned(file.folder, 0, () => Promise.reject(new Error("A folder")));
      item.skip = "A folder is not opened. Choose the files in it, or zip it.";
      added.push(item);
    } else if (isZip(file.name)) {
      added.push(...(await openZip(file)));
    } else {
      added.push(planned(file.name, file.size, () => Promise.resolve(file)));
    }
  }
  let uploadable = already.filter((item) => !item.skip).length;
  for (const item of added) {
    if (item.skip) continue;
    if (uploadable >= MAX_BATCH_FILES) {
      item.skip = `Over the limit of ${MAX_BATCH_FILES} files at a time. Upload it next.`;
    } else {
      uploadable += 1;
    }
  }
  return [...already, ...added];
}
