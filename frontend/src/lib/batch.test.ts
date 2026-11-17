import { describe, expect, it } from "vitest";

import { PYTHON_ZIP, buildZip, fromBase64, zipFile } from "../test/zips";
import {
  MAX_BATCH_FILES,
  addPlanned,
  isSystemFile,
  planFiles,
  uploadKey,
  type PlannedFile,
} from "./batch";

const file = (name: string, content = "content", type = "") => new File([content], name, { type });
const pick = (items: PlannedFile[]) =>
  items.map((item) => ({ name: item.name, from: item.from, skip: item.skip }));

describe("planFiles", () => {
  it("plans each chosen file as a document titled from its name", async () => {
    const pdf = file("exam_regulations-2024.pdf");
    const [item] = await planFiles([pdf]);
    expect(item).toMatchObject({ name: "exam_regulations-2024.pdf", title: "Exam regulations 2024" });
    expect(item.skip).toBeUndefined();
    expect(await item.load()).toBe(pdf);
  });

  it("lists what it will not upload, with the reason, without dropping the rest", async () => {
    const items = await planFiles([file("a.pdf"), file("budget.xlsm"), file("empty.txt", "")]);
    expect(items[0].skip).toBeUndefined();
    expect(items[1].skip).toMatch(/Unsupported file type/);
    expect(items[2].skip).toMatch(/empty/);
  });

  it("opens a zip: its files in order, folders and system files left out", async () => {
    const items = await planFiles([zipFile(fromBase64(PYTHON_ZIP), "term.zip")]);
    expect(pick(items)).toEqual([
      { name: "exam_rules.txt", from: "term.zip › reports", skip: undefined },
      { name: "scan.png", from: "term.zip", skip: undefined },
      { name: "résumé.txt", from: "term.zip", skip: undefined },
      { name: "forced64.txt", from: "term.zip", skip: undefined },
      { name: "sheet.xlsx", from: "term.zip", skip: undefined },
    ]);
  });

  it("lists what in a zip it will not upload, with the reason, and still takes the rest", async () => {
    const zip = await buildZip([
      { name: "rules.pdf", data: "pdf bytes" },
      { name: "macros.xlsm", data: "old excel" },
      { name: "notes.txt", data: "notes" },
    ]);
    const items = await planFiles([zipFile(zip, "mixed.zip")]);
    expect(pick(items)).toEqual([
      { name: "rules.pdf", from: "mixed.zip", skip: undefined },
      { name: "macros.xlsm", from: "mixed.zip", skip: expect.stringMatching(/Unsupported/) },
      { name: "notes.txt", from: "mixed.zip", skip: undefined },
    ]);
  });

  it("hands over a file from a zip with its content and a type for its extension", async () => {
    const [rules, scan] = await planFiles([zipFile(fromBase64(PYTHON_ZIP))]);
    const loaded = await rules.load();
    expect(loaded.name).toBe("exam_rules.txt");
    expect(loaded.type).toBe("text/plain");
    expect(new TextDecoder().decode(await loaded.arrayBuffer())).toMatch(/^Exam rules: 75 percent/);
    expect((await scan.load()).type).toBe("image/png");
  });

  it("skips what a zip holds but cannot be uploaded from it", async () => {
    const zip = await buildZip([
      { name: "inner.zip", data: "PK" },
      { name: "secret.pdf", data: "x", flags: 0x0001 },
      { name: "link.pdf", data: "elsewhere.pdf", madeBy: 3, unixMode: 0o120777 },
      { name: "old.pdf", data: "x", method: 12 },
    ]);
    const reasons = (await planFiles([zipFile(zip)])).map((item) => item.skip);
    expect(reasons).toEqual([
      expect.stringMatching(/zip inside a zip/),
      expect.stringMatching(/password/),
      expect.stringMatching(/link/),
      expect.stringMatching(/cannot open/),
    ]);
  });

  it("lists a zip it cannot open, or one with nothing in it, as one skipped entry", async () => {
    const [broken] = await planFiles([file("broken.zip", "not a zip")]);
    expect(broken).toMatchObject({ name: "broken.zip", skip: expect.stringMatching(/damaged/) });
    await expect(broken.load()).rejects.toThrow();

    const onlyFolders = await buildZip([{ name: "a/", data: "", method: 0 }, { name: "__MACOSX/._x", data: "j" }]);
    const [empty] = await planFiles([zipFile(onlyFolders, "empty.zip")]);
    expect(empty).toMatchObject({ name: "empty.zip", skip: "This zip has no files in it." });
  });

  it("says a dropped folder is not opened", async () => {
    const [folder] = await planFiles([{ folder: "Minutes" }]);
    expect(folder).toMatchObject({ name: "Minutes", skip: expect.stringMatching(/folder/) });
  });

  it("keeps titles within the server's limit", async () => {
    const [item] = await planFiles([file(`${"long ".repeat(80)}.pdf`)]);
    expect(item.title.length).toBe(255);
  });

  it("identifies a file by what it is, and a zipped file by its content wherever it is", async () => {
    const pdf = new File(["x"], "a.pdf", { lastModified: 1000 });
    const [one] = await planFiles([pdf]);
    const [again] = await planFiles([pdf]);
    const [edited] = await planFiles([new File(["x"], "a.pdf", { lastModified: 2000 })]);
    expect(again.identity).toBe(one.identity);
    expect(again.id).not.toBe(one.id);
    expect(edited.identity).not.toBe(one.identity);

    const zipped = await buildZip([{ name: "x/notes.txt", data: "same" }]);
    const rezipped = await buildZip([{ name: "other/notes.txt", data: "same" }]);
    const changed = await buildZip([{ name: "x/notes.txt", data: "different" }]);
    const [a] = await planFiles([zipFile(zipped, "one.zip")]);
    const [b] = await planFiles([zipFile(rezipped, "two.zip")]);
    const [c] = await planFiles([zipFile(changed, "one.zip")]);
    expect(b.identity).toBe(a.identity);
    expect(c.identity).not.toBe(a.identity);
  });
});

describe("uploadKey", () => {
  it("is the same for the same file and settings, and differs when either does", async () => {
    const key = await uploadKey("file a", '["A","GENERAL",""]');
    expect(key).toMatch(/^[0-9a-f]{64}$/);
    expect(await uploadKey("file a", '["A","GENERAL",""]')).toBe(key);
    expect(await uploadKey("file b", '["A","GENERAL",""]')).not.toBe(key);
    expect(await uploadKey("file a", '["A","POLICY",""]')).not.toBe(key);
  });
});

describe("addPlanned", () => {
  it("adds after what is listed, leaving out a file already there", async () => {
    const pdf = file("a.pdf");
    const first = await planFiles([pdf]);
    const next = addPlanned(first, await planFiles([pdf, file("b.pdf")]));
    expect(Array.isArray(next) && next.map((item) => item.name)).toEqual(["a.pdf", "b.pdf"]);
  });

  it(`refuses a whole choice that would pass ${MAX_BATCH_FILES} files, rather than half of it`, async () => {
    const many = await planFiles(Array.from({ length: MAX_BATCH_FILES - 1 }, (_, i) => file(`doc-${i}.txt`)));
    const fits = addPlanned(many, await planFiles([file("last.txt"), file("bad.xlsm")]));
    expect(Array.isArray(fits) && fits.length).toBe(MAX_BATCH_FILES + 1);

    const refused = addPlanned(many, await planFiles([file("x.txt"), file("y.txt")]));
    expect(refused).toBe(
      `That would be ${MAX_BATCH_FILES + 1} files to upload, and at most ${MAX_BATCH_FILES} go at a time. Choose fewer, or split the zip.`,
    );
  });
});

describe("isSystemFile", () => {
  it("knows what Finder and Explorer add unasked", () => {
    for (const path of ["__MACOSX/a.pdf", "docs/__MACOSX/x", "docs/._a.pdf", ".DS_Store", "a/Thumbs.db", "desktop.ini"]) {
      expect(isSystemFile(path)).toBe(true);
    }
    for (const path of ["a.pdf", "docs/notes.txt", "_private.pdf", "macosx/a.pdf"]) {
      expect(isSystemFile(path)).toBe(false);
    }
  });
});
