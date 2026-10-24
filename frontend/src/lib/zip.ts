/**
 * Reads the files inside a .zip, in the browser, one at a time.
 *
 * Why in the browser: each file inside is then uploaded exactly as if it
 * had been chosen on its own, through the same endpoint - so it meets the
 * same admission check, idempotency key, audit row and malware scan, and
 * the server never parses an archive a user supplied (INGESTION refuses a
 * plain .zip by design).
 *
 * Why no library: the Content-Security-Policy (script-src 'self') refuses
 * the blob: workers that fflate's and zip.js's asynchronous paths start, and
 * their synchronous paths hold the whole archive in memory and freeze the
 * page while it inflates. The format needs little: the central directory at
 * the end says where each file is, and the browser's own
 * DecompressionStream inflates it, streaming and off the main thread.
 *
 * Only the central directory is read up front. A file's bytes are read when
 * it is its turn to upload, so a 1 GB archive costs about one file's memory
 * at a time.
 *
 * Refused, each with a reason a person can act on: archives split across
 * several files, encrypted entries, compression other than stored or
 * deflate (deflate64, bzip2, LZMA, zstd), and entries whose data does not
 * match its declared size or CRC-32.
 */

const EOCD = 0x06054b50;
const ZIP64_LOCATOR = 0x07064b50;
const ZIP64_EOCD = 0x06064b50;
const CENTRAL_HEADER = 0x02014b50;
const LOCAL_HEADER = 0x04034b50;
const MAX16 = 0xffff;
const MAX32 = 0xffffffff;

const STORED = 0;
const DEFLATED = 8;

/** Past this many entries an archive is refused rather than listed. */
export const MAX_ZIP_ENTRIES = 10_000;
// A central directory is about 100 bytes an entry; this is far past
// MAX_ZIP_ENTRIES of them and still a bounded read.
const MAX_DIRECTORY_BYTES = 64 * 1024 * 1024;

export class ZipError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ZipError";
  }
}

const DAMAGED = "This zip is damaged or is not a zip file.";
const NO_INFLATE = "This browser cannot open zip files. Unzip it and choose the files instead.";
const UNREADABLE =
  "The zip could not be read again; it may have been moved or changed since it was chosen.";

export interface ZipEntry {
  /** The path inside the archive, "/"-separated. */
  path: string;
  /** The last part of the path: what the file is called. */
  name: string;
  /** Uncompressed size, as the archive declares it. */
  size: number;
  compressedSize: number;
  method: number;
  crc32: number;
  /** Where the entry's local header starts. */
  offset: number;
  encrypted: boolean;
  directory: boolean;
  /** A Unix symbolic link: its "content" is the path it points at. */
  link: boolean;
}

/** True if the browser can inflate this entry. */
export function canRead(entry: ZipEntry): boolean {
  return entry.method === STORED || entry.method === DEFLATED;
}

async function view(blob: Blob, start: number, end: number): Promise<DataView> {
  return new DataView(await blob.slice(start, end).arrayBuffer());
}

/** A ZIP64 field. Exact up to 2^53, far past any file a browser can hold. */
function uint64(data: DataView, at: number): number {
  return data.getUint32(at, true) + data.getUint32(at + 4, true) * 2 ** 32;
}

// The upper half of code page 437, the encoding the format specifies for a
// name without the UTF-8 flag. TextDecoder has no label for it.
const CP437 =
  "ÇüéâäàåçêëèïîìÄÅÉæÆôöòûùÿÖÜ¢£¥₧ƒáíóúñÑªº¿⌐¬½¼¡«»░▒▓│┤╡╢╖╕╣║╗╝╜╛┐" +
  "└┴┬├─┼╞╟╚╔╩╦╠═╬╧╨╤╥╙╘╒╓╫╪┘┌█▄▌▐▀αßΓπΣσµτΦΘΩδ∞φε∩≡±≥≤⌠⌡÷≈°∙·√ⁿ²■ ";

function decodeName(raw: Uint8Array, utf8Flag: boolean): string {
  try {
    // Many tools write UTF-8 without setting the flag, and a CP437 name
    // that happens to be valid UTF-8 is rare outside plain ASCII.
    return new TextDecoder("utf-8", { fatal: !utf8Flag }).decode(raw);
  } catch {
    return Array.from(raw, (byte) => (byte < 0x80 ? String.fromCharCode(byte) : CP437[byte - 0x80])).join("");
  }
}

/** Lists what an archive holds, reading only its central directory. */
export async function listZip(file: Blob): Promise<ZipEntry[]> {
  // The end record is the last 22 bytes, unless a comment (up to 64 KiB)
  // follows it - so it is found by searching back from the end. The
  // comment can itself contain the signature; the real record is the one
  // whose comment length reaches exactly to the end of the file, and only
  // failing that (trailing bytes after the comment) the last that fits.
  const tailStart = Math.max(0, file.size - (22 + MAX16));
  const tail = await view(file, tailStart, file.size);
  let end = -1;
  for (let at = tail.byteLength - 22; at >= 0; at--) {
    if (tail.getUint32(at, true) !== EOCD) continue;
    const reach = at + 22 + tail.getUint16(at + 20, true);
    if (reach === tail.byteLength) {
      end = at;
      break;
    }
    if (end < 0 && reach <= tail.byteLength) end = at;
  }
  if (end < 0) throw new ZipError(DAMAGED);

  const endAt = tailStart + end;
  let disk = tail.getUint16(end + 4, true);
  let directoryDisk = tail.getUint16(end + 6, true);
  let count = tail.getUint16(end + 10, true);
  let directorySize = tail.getUint32(end + 12, true);
  let directoryOffset = tail.getUint32(end + 16, true);

  // The ZIP64 locator sits just before the end record: in the tail, unless
  // a comment of nearly 64 KiB pushed it out.
  let locator: DataView | null = null;
  if (end >= 20) locator = new DataView(tail.buffer, tail.byteOffset + end - 20, 20);
  else if (endAt >= 20) locator = await view(file, endAt - 20, endAt);
  const zip64 = locator?.getUint32(0, true) === ZIP64_LOCATOR;
  if (locator && zip64) {
    const recordOffset = uint64(locator, 8);
    const record = await view(file, recordOffset, recordOffset + 56);
    if (record.byteLength < 56 || record.getUint32(0, true) !== ZIP64_EOCD) {
      throw new ZipError(DAMAGED);
    }
    disk = record.getUint32(16, true);
    directoryDisk = record.getUint32(20, true);
    count = uint64(record, 32);
    directorySize = uint64(record, 40);
    directoryOffset = uint64(record, 48);
  }

  if (disk !== 0 || directoryDisk !== 0) {
    throw new ZipError("This zip is split across several files. Join it into one and try again.");
  }
  if (count > MAX_ZIP_ENTRIES) {
    throw new ZipError(
      `This zip holds ${count.toLocaleString("en")} files; at most ${MAX_ZIP_ENTRIES.toLocaleString("en")} can be opened.`,
    );
  }
  // Bytes in front of the archive - a self-extracting stub, a zip appended
  // to another file - shift every offset it records by their length. Where
  // the directory really starts is known: right before the end record. (Not
  // for ZIP64, whose own record sits between them.)
  let shift = 0;
  if (!zip64) {
    shift = endAt - directorySize - directoryOffset;
    if (shift < 0) throw new ZipError(DAMAGED);
    directoryOffset += shift;
  }
  if (directorySize > MAX_DIRECTORY_BYTES || directoryOffset + directorySize > file.size) {
    throw new ZipError(DAMAGED);
  }

  const directory = await view(file, directoryOffset, directoryOffset + directorySize);
  const entries: ZipEntry[] = [];
  let at = 0;
  for (let index = 0; index < count; index++) {
    if (at + 46 > directory.byteLength || directory.getUint32(at, true) !== CENTRAL_HEADER) {
      throw new ZipError(DAMAGED);
    }
    const madeBy = directory.getUint16(at + 4, true) >> 8;
    const flags = directory.getUint16(at + 8, true);
    const method = directory.getUint16(at + 10, true);
    const crc32 = directory.getUint32(at + 16, true);
    let compressedSize = directory.getUint32(at + 20, true);
    let size = directory.getUint32(at + 24, true);
    const nameLength = directory.getUint16(at + 28, true);
    const extraLength = directory.getUint16(at + 30, true);
    const commentLength = directory.getUint16(at + 32, true);
    const unixMode = directory.getUint32(at + 38, true) >>> 16;
    let offset = directory.getUint32(at + 42, true);

    const nameStart = at + 46;
    const extraStart = nameStart + nameLength;
    const next = extraStart + extraLength + commentLength;
    if (next > directory.byteLength) throw new ZipError(DAMAGED);

    // ZIP64: a field too big for 32 bits reads 0xFFFFFFFF, and the real
    // value is in extra field 0x0001 - holding only the fields that
    // overflowed, in this order.
    if (size === MAX32 || compressedSize === MAX32 || offset === MAX32) {
      for (let field = extraStart; field + 4 <= extraStart + extraLength; ) {
        const id = directory.getUint16(field, true);
        const length = directory.getUint16(field + 2, true);
        if (id === 0x0001) {
          let value = field + 4;
          const stop = value + length;
          if (size === MAX32 && value + 8 <= stop) {
            size = uint64(directory, value);
            value += 8;
          }
          if (compressedSize === MAX32 && value + 8 <= stop) {
            compressedSize = uint64(directory, value);
            value += 8;
          }
          if (offset === MAX32 && value + 8 <= stop) offset = uint64(directory, value);
          break;
        }
        field += 4 + length;
      }
    }

    const raw = new Uint8Array(directory.buffer, directory.byteOffset + nameStart, nameLength);
    const path = decodeName(raw, (flags & 0x0800) !== 0).replace(/\\/g, "/");
    entries.push({
      path,
      name: path.split("/").filter(Boolean).pop() ?? "",
      size,
      compressedSize,
      method,
      crc32,
      offset: offset + shift,
      encrypted: (flags & 0x0001) !== 0,
      directory: path.endsWith("/"),
      link: madeBy === 3 && (unixMode & 0o170000) === 0o120000,
    });
    at = next;
  }
  // 65,536 entries or more, written without ZIP64, wrap the end record's
  // 16-bit count around; another header after the last one counted means
  // this listing would be part of the archive, presented as all of it.
  if (at + 4 <= directory.byteLength && directory.getUint32(at, true) === CENTRAL_HEADER) {
    throw new ZipError(
      `This zip holds more files than it says; at most ${MAX_ZIP_ENTRIES.toLocaleString("en")} can be opened.`,
    );
  }
  return entries;
}

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();

/** CRC-32 (the zip and gzip one), continued from `crc` for streamed data. */
export function crc32(data: Uint8Array, crc = 0): number {
  let c = (crc ^ MAX32) >>> 0;
  for (let i = 0; i < data.length; i++) c = CRC_TABLE[(c ^ data[i]) & 0xff] ^ (c >>> 8);
  return (c ^ MAX32) >>> 0;
}

const CHUNK_BYTES = 1024 * 1024;

/**
 * Part of a blob as a stream, a megabyte at a time. Through slice() rather
 * than Blob.stream(), which jsdom - where the unit tests run - lacks.
 */
function sliceStream(blob: Blob, start: number, end: number): ReadableStream<Uint8Array> {
  let at = start;
  return new ReadableStream<Uint8Array>({
    async pull(controller) {
      if (at >= end) {
        controller.close();
        return;
      }
      const next = Math.min(end, at + CHUNK_BYTES);
      controller.enqueue(new Uint8Array(await blob.slice(at, next).arrayBuffer()));
      at = next;
    },
  });
}

/**
 * One entry's content. Refuses, rather than returns, anything that would
 * not be the file that was zipped: a size over `limit` (checked as it
 * inflates, since the declared size is only a claim), or data that does
 * not match the declared size and CRC-32.
 */
export async function readZipEntry(file: Blob, entry: ZipEntry, limit: number): Promise<Blob> {
  if (entry.encrypted) throw new ZipError("It is password-protected.");
  if (!canRead(entry)) {
    throw new ZipError(`It is compressed in a way the browser cannot open (method ${entry.method}).`);
  }
  if (entry.method === DEFLATED && typeof DecompressionStream === "undefined") {
    throw new ZipError(NO_INFLATE);
  }

  const local = await view(file, entry.offset, entry.offset + 30);
  if (local.byteLength < 30 || local.getUint32(0, true) !== LOCAL_HEADER) {
    throw new ZipError(DAMAGED);
  }
  // The local header's name and extra field can differ in length from the
  // central directory's copy, so the data starts where THIS header ends.
  const start = entry.offset + 30 + local.getUint16(26, true) + local.getUint16(28, true);
  const end = start + entry.compressedSize;
  if (end > file.size) throw new ZipError(DAMAGED);

  let stream = sliceStream(file, start, end);
  if (entry.method === DEFLATED) {
    let inflate: TransformStream<Uint8Array, Uint8Array>;
    try {
      // Typed as taking any BufferSource, which TypeScript will not match
      // to the Uint8Array chunks piped into it.
      inflate = new DecompressionStream("deflate-raw") as unknown as TransformStream<
        Uint8Array,
        Uint8Array
      >;
    } catch {
      // Chrome 80-102 have DecompressionStream, but not "deflate-raw".
      throw new ZipError(NO_INFLATE);
    }
    stream = stream.pipeThrough(inflate);
  }
  const reader = stream.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  let crc = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > entry.size || total > limit) {
        await reader.cancel().catch(() => undefined);
        throw new ZipError("It is damaged inside the zip.");
      }
      crc = crc32(value, crc);
      chunks.push(value);
    }
  } catch (caught) {
    if (caught instanceof ZipError) throw caught;
    // The zip could not be read again (moved, changed or deleted since it
    // was chosen) - not data that would not inflate.
    if (caught instanceof DOMException && ["NotReadableError", "NotFoundError"].includes(caught.name)) {
      throw new ZipError(UNREADABLE);
    }
    throw new ZipError("It is damaged inside the zip.");
  }
  if (total !== entry.size || crc !== entry.crc32) {
    throw new ZipError("It is damaged inside the zip.");
  }
  return new Blob(chunks as BlobPart[]);
}
