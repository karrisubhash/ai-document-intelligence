import { afterEach, describe, expect, it, vi } from "vitest";

import {
  BSDTAR_ZIP,
  EXPLORER_ZIP,
  POWERSHELL_ZIP,
  PYTHON_STREAMED_ZIP,
  PYTHON_ZIP,
  buildZip,
  fromBase64,
} from "../test/zips";
import { MAX_ZIP_ENTRIES, ZipError, canRead, crc32, listZip, readZipEntry, type ZipEntry } from "./zip";

const LIMIT = 50 * 1024 * 1024;

const text = async (blob: Blob) => new TextDecoder().decode(await blob.arrayBuffer());
const byPath = (entries: ZipEntry[], path: string) => {
  const entry = entries.find((item) => item.path === path);
  if (!entry) throw new Error(`no ${path} in ${entries.map((item) => item.path).join(", ")}`);
  return entry;
};

describe("zips made by real tools", () => {
  it("reads Python's zipfile: deflated, stored, UTF-8 names, folders, a ZIP64 local header", async () => {
    const zip = fromBase64(PYTHON_ZIP);
    const entries = await listZip(zip);
    expect(entries.map((entry) => entry.path)).toEqual([
      "reports/",
      "reports/exam_rules.txt",
      "scan.png",
      "résumé.txt",
      "__MACOSX/._scan.png",
      "forced64.txt",
      "sheet.xlsx",
    ]);
    expect(byPath(entries, "reports/").directory).toBe(true);

    const rules = byPath(entries, "reports/exam_rules.txt");
    expect(rules).toMatchObject({ name: "exam_rules.txt", method: 8, directory: false });
    expect(await text(await readZipEntry(zip, rules, LIMIT))).toBe(
      "Exam rules: 75 percent attendance is required.\n".repeat(20),
    );

    const scan = new Uint8Array(await (await readZipEntry(zip, byPath(entries, "scan.png"), LIMIT)).arrayBuffer());
    expect(Array.from(scan.slice(0, 8))).toEqual([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
    expect(scan.length).toBe(72);

    expect(await text(await readZipEntry(zip, byPath(entries, "résumé.txt"), LIMIT))).toBe("Ünïcödé name");
    // The local header carries a ZIP64 extra field the central one does not.
    expect(await text(await readZipEntry(zip, byPath(entries, "forced64.txt"), LIMIT))).toBe(
      "zip64 local header\n".repeat(10),
    );
  });

  it("reads a streamed zip, whose sizes come after the data", async () => {
    const zip = fromBase64(PYTHON_STREAMED_ZIP);
    const [entry] = await listZip(zip);
    expect(entry.path).toBe("streamed.txt");
    expect(await text(await readZipEntry(zip, entry, LIMIT))).toBe(
      "streamed with a data descriptor\n".repeat(30),
    );
  });

  it("reads Windows PowerShell's Compress-Archive, which writes backslashes", async () => {
    const zip = fromBase64(POWERSHELL_ZIP);
    const entries = await listZip(zip);
    expect(entries.map((entry) => [entry.path, entry.name])).toEqual([
      ["sub/minutes.txt", "minutes.txt"],
      ["notice.txt", "notice.txt"],
    ]);
    expect(await text(await readZipEntry(zip, entries[0], LIMIT))).toBe(
      "Minutes of the board meeting.\n".repeat(10),
    );
  });

  it("reads Windows Explorer's compressed folder, whose names are code page 437", async () => {
    const zip = fromBase64(EXPLORER_ZIP);
    const entries = await listZip(zip);
    expect(entries.map((entry) => entry.path)).toEqual(["Café menu.txt", "Minutes/board.txt"]);
    expect(await text(await readZipEntry(zip, entries[0], LIMIT))).toBe(
      "Lunch is served from noon.\n".repeat(8),
    );
  });

  it("reads Windows tar (bsdtar), with data descriptors and Unix extra fields", async () => {
    const zip = fromBase64(BSDTAR_ZIP);
    const entries = await listZip(zip);
    expect(entries.map((entry) => entry.path)).toEqual(["notice.txt", "sub/", "sub/minutes.txt"]);
    expect(await text(await readZipEntry(zip, byPath(entries, "notice.txt"), LIMIT))).toBe(
      "Library closes at 8 pm.\n".repeat(10),
    );
  });
});

describe("listZip", () => {
  it("finds the directory behind a comment that holds a record of its own", async () => {
    // A fake end record inside the comment, whose own comment length fits
    // in the file: the first match searching back, and a wrong one.
    const fake = "PK\u0005\u0006" + "\u0000".repeat(16) + "\u0002\u0000";
    const zip = await buildZip([{ name: "a.txt", data: "alpha" }], { comment: `${fake} and more after it` });
    const entries = await listZip(zip);
    expect(entries).toHaveLength(1);
    expect(await text(await readZipEntry(zip, entries[0], LIMIT))).toBe("alpha");
  });

  it("finds the ZIP64 record behind a comment of nearly 64 KiB", async () => {
    const zip = await buildZip([{ name: "a.txt", data: "alpha", zip64: true }], {
      zip64: true,
      comment: "c".repeat(65_530),
    });
    const [entry] = await listZip(zip);
    expect(await text(await readZipEntry(zip, entry, LIMIT))).toBe("alpha");
  });

  it("reads a zip with bytes in front of it, as a self-extracting one has", async () => {
    const zip = await buildZip([{ name: "a.txt", data: "alpha" }, { name: "b.txt", data: "beta" }]);
    const stubbed = new Blob([new Uint8Array(100).fill(0x4d), zip]);
    const entries = await listZip(stubbed);
    expect(await text(await readZipEntry(stubbed, entries[1], LIMIT))).toBe("beta");
  });

  it("reads ZIP64 sizes and offsets", async () => {
    const zip = await buildZip(
      [
        { name: "first.txt", data: "one" },
        { name: "second.txt", data: "two".repeat(100), zip64: true },
      ],
      { zip64: true },
    );
    const entries = await listZip(zip);
    expect(entries[1]).toMatchObject({ name: "second.txt", size: 300 });
    expect(await text(await readZipEntry(zip, entries[1], LIMIT))).toBe("two".repeat(100));
  });

  it("decodes a legacy name as code page 437", async () => {
    // "Café" as DOS wrote it: 0x82 is é in CP437 and invalid as UTF-8.
    const zip = await buildZip([{ rawName: Uint8Array.from([0x43, 0x61, 0x66, 0x82, 0x2e, 0x74, 0x78, 0x74]), data: "x" }]);
    expect((await listZip(zip))[0].name).toBe("Café.txt");
  });

  it("marks encrypted entries, links and compression it cannot open", async () => {
    const zip = await buildZip([
      { name: "secret.pdf", data: "x", flags: 0x0001 },
      { name: "link.pdf", data: "target.pdf", madeBy: 3, unixMode: 0o120777 },
      { name: "old.pdf", data: "x", method: 12 },
    ]);
    const [secret, link, old] = await listZip(zip);
    expect(secret.encrypted).toBe(true);
    expect(link.link).toBe(true);
    expect(canRead(old)).toBe(false);
    await expect(readZipEntry(zip, secret, LIMIT)).rejects.toThrow(/password/);
    await expect(readZipEntry(zip, old, LIMIT)).rejects.toThrow(/method 12/);
  });

  it("refuses what is not a zip, a split zip and an absurd number of entries", async () => {
    await expect(listZip(new Blob(["just text"]))).rejects.toThrow(ZipError);
    await expect(listZip(new Blob([]))).rejects.toThrow(ZipError);
    await expect(listZip(await buildZip([{ name: "a.txt", data: "a" }], { disk: 1 }))).rejects.toThrow(/split/);
    await expect(
      listZip(await buildZip([{ name: "a.txt", data: "a" }], { count: MAX_ZIP_ENTRIES + 1 })),
    ).rejects.toThrow(/10,001 files/);
  });

  it("refuses a directory with more entries than its end record counts", async () => {
    // What 65,536 entries written without ZIP64 look like: the count wraps.
    const zip = await buildZip([{ name: "a.txt", data: "a" }, { name: "b.txt", data: "b" }], { count: 1 });
    await expect(listZip(zip)).rejects.toThrow(/more files than it says/);
  });

  it("refuses a directory that runs past its entries", async () => {
    await expect(listZip(await buildZip([{ name: "a.txt", data: "a" }], { count: 2 }))).rejects.toThrow(
      /damaged/,
    );
  });
});

describe("readZipEntry", () => {
  it("refuses data whose CRC-32 does not match", async () => {
    const zip = await buildZip([{ name: "a.txt", data: "hello", crc: 0x12345678 }]);
    const [entry] = await listZip(zip);
    await expect(readZipEntry(zip, entry, LIMIT)).rejects.toThrow(/damaged/);
  });

  it("stops reading once the output passes the declared size", async () => {
    // Claiming 10 bytes is how a bomb gets past a check made on the header
    // alone. Stopping at the first chunk past it is what bounds memory; the
    // size check after the end would refuse it too, having read it all.
    const data = new Uint8Array(3 * 1024 * 1024 + 17);
    const zip = await buildZip([{ name: "bomb.txt", data, method: 0, declaredSize: 10 }]);
    class CountingBlob extends Blob {
      reads = 0;
      slice(start?: number, end?: number, type?: string) {
        this.reads += 1;
        return super.slice(start, end, type);
      }
    }
    const counted = new CountingBlob([zip]);
    const [entry] = await listZip(counted);
    counted.reads = 0;
    await expect(readZipEntry(counted, entry, LIMIT)).rejects.toThrow(/damaged/);
    // The local header, then at most two of the four megabyte chunks.
    expect(counted.reads).toBeLessThanOrEqual(3);
  });

  it("refuses a deflated file that inflates past its declared size", async () => {
    // A megabyte of zeros deflates to about a kilobyte.
    const zip = await buildZip([{ name: "bomb.txt", data: new Uint8Array(1024 * 1024), declaredSize: 10 }]);
    const [entry] = await listZip(zip);
    await expect(readZipEntry(zip, entry, LIMIT)).rejects.toThrow(/damaged/);
  });

  it("refuses an entry larger than the limit even when it is honest about it", async () => {
    const zip = await buildZip([{ name: "big.txt", data: new Uint8Array(2048) }]);
    const [entry] = await listZip(zip);
    await expect(readZipEntry(zip, entry, 1024)).rejects.toThrow(ZipError);
    expect((await readZipEntry(zip, entry, 4096)).size).toBe(2048);
  });

  it("reads a file larger than one read chunk", async () => {
    const data = Uint8Array.from({ length: 3 * 1024 * 1024 + 17 }, (_, i) => (i * 31) % 251);
    const zip = await buildZip([{ name: "big.bin", data, method: 0 }, { name: "big.z", data }]);
    for (const entry of await listZip(zip)) {
      const read = new Uint8Array(await (await readZipEntry(zip, entry, LIMIT)).arrayBuffer());
      expect(read.length).toBe(data.length);
      // A loop, not toEqual, which takes seconds a megabyte.
      expect(read.every((byte, i) => byte === data[i])).toBe(true);
    }
  });
});

describe("readZipEntry in older browsers", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("says the browser cannot open zips when it has no deflate-raw", async () => {
    const zip = await buildZip([{ name: "a.txt", data: "alpha" }]);
    const [entry] = await listZip(zip);
    // Chrome 80-102: the class exists, the format does not.
    vi.stubGlobal(
      "DecompressionStream",
      class {
        constructor() {
          throw new TypeError("Unsupported compression format: 'deflate-raw'");
        }
      },
    );
    await expect(readZipEntry(zip, entry, LIMIT)).rejects.toThrow(/browser cannot open zip files/);
  });
});

describe("crc32", () => {
  it("gives the standard check value, whole or in pieces", () => {
    // CRC-32/ISO-HDLC of "123456789", the value every implementation is checked against.
    const data = new TextEncoder().encode("123456789");
    expect(crc32(data)).toBe(0xcbf43926);
    expect(crc32(data.subarray(4), crc32(data.subarray(0, 4)))).toBe(0xcbf43926);
    expect(crc32(new Uint8Array())).toBe(0);
  });
});
