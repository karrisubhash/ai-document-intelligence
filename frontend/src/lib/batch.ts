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

/**
 * The most files one dialog uploads. A choice that would pass it is refused
 * whole rather than cut short: a zip half-listed left its other half with
 * no way into the dialog but choosing the zip again.
 */
export const MAX_BATCH_FILES = 200;

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
  /** What makes two choices the same file: the source of its idempotency key. */
  identity: string;
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

const hex = (bytes: Uint8Array) =>
  Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");

/** 128 random bits as hex. crypto.randomUUID needs a secure context; this does not. */
export function randomKey(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return hex(bytes);
}

/**
 * The Idempotency-Key for uploading this file with these settings (title,
 * type, department). Derived, not random: the same file chosen again - in
 * this dialog or a later one, within the server's 24 hours - gets the same
 * key, and the server answers with the document it already made instead of
 * a copy. So choosing a zip again after half of it went through uploads
 * only the other half. Any change of settings is a different key, as the
 * server would refuse the old one for a different request (422).
 *
 * Without SubtleCrypto (a page served over plain http) the key is random:
 * a retry in the same dialog still cannot duplicate, a later choice can.
 */
export async function uploadKey(identity: string, settings: string): Promise<string> {
  if (!globalThis.crypto?.subtle) return randomKey();
  const data = new TextEncoder().encode(`${identity}\u0000${settings}`);
  return hex(new Uint8Array(await crypto.subtle.digest("SHA-256", data)));
}

function planned(
  name: string,
  size: number,
  identity: string,
  load: () => Promise<File>,
  from?: string,
): PlannedFile {
  return {
    id: randomKey(),
    name,
    // The server's limit; a long file name is not a reason to refuse.
    title: titleFromFilename(name).slice(0, 255),
    size,
    from,
    skip: fileProblem(name, size) ?? undefined,
    identity,
    load,
  };
}

const fileIdentity = (file: File) => `file\u0000${file.name}\u0000${file.size}\u0000${file.lastModified}`;

function fromEntry(archive: File, entry: ZipEntry): PlannedFile {
  const folder = entry.path.slice(0, entry.path.length - entry.name.length).replace(/\/$/, "");
  const from = folder ? `${archive.name} › ${folder}` : archive.name;
  const item = planned(
    entry.name,
    entry.size,
    // Content, not place: the same file zipped again, or in another zip,
    // is the same upload.
    `entry\u0000${entry.name}\u0000${entry.size}\u0000${entry.crc32}`,
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
  const unusable = (reason: string) => {
    const item = planned(archive.name, archive.size, fileIdentity(archive), () =>
      Promise.reject(new ZipError(reason)),
    );
    item.skip = reason;
    return [item];
  };
  let entries: ZipEntry[];
  try {
    entries = await listZip(archive);
  } catch (caught) {
    return unusable(caught instanceof ZipError ? caught.message : "This zip could not be opened.");
  }
  const files = entries
    .filter((entry) => !entry.directory && !isSystemFile(entry.path))
    .map((entry) => fromEntry(archive, entry));
  return files.length ? files : unusable("This zip has no files in it.");
}

/** A dropped folder, which the browser hands over as an empty, typeless file. */
export interface PickedFolder {
  folder: string;
}

/** Plans what was picked, in the order chosen; a zip's files in its own order. */
export async function planFiles(picked: (File | PickedFolder)[]): Promise<PlannedFile[]> {
  const plans: PlannedFile[] = [];
  for (const file of picked) {
    if (!(file instanceof File)) {
      const item = planned(file.folder, 0, `folder\u0000${file.folder}`, () =>
        Promise.reject(new Error("A folder")),
      );
      item.skip = "A folder is not opened. Choose the files in it, or zip it.";
      plans.push(item);
    } else if (isZip(file.name)) {
      plans.push(...(await openZip(file)));
    } else {
      plans.push(planned(file.name, file.size, fileIdentity(file), () => Promise.resolve(file)));
    }
  }
  return plans;
}

/**
 * `current` with `added` after it, leaving out what is already listed (the
 * same file twice is one document). A message instead, and nothing added,
 * if that would pass MAX_BATCH_FILES files to upload.
 */
export function addPlanned(current: PlannedFile[], added: PlannedFile[]): PlannedFile[] | string {
  const seen = new Set(current.map((file) => file.identity));
  const fresh = added.filter((file) => !seen.has(file.identity) && seen.add(file.identity));
  const next = [...current, ...fresh];
  const uploadable = next.filter((file) => !file.skip).length;
  if (uploadable > MAX_BATCH_FILES) {
    return `That would be ${uploadable.toLocaleString("en")} files to upload, and at most ${MAX_BATCH_FILES} go at a time. Choose fewer, or split the zip.`;
  }
  return next;
}
