import { describe, expect, it } from "vitest";

import { PYTHON_ZIP, buildZip, fromBase64, zipFile } from "../test/zips";
import { MAX_BATCH_FILES, isSystemFile, planFiles, type PlannedFile } from "./batch";

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
    const items = await planFiles([file("a.pdf"), file("budget.xlsx"), file("empty.txt", "")]);
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
      { name: "sheet.xlsx", from: "term.zip", skip: expect.stringMatching(/Unsupported/) },
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

  it(`uploads at most ${MAX_BATCH_FILES} at a time, counting what is already chosen`, async () => {
    const many = Array.from({ length: MAX_BATCH_FILES + 2 }, (_, i) => file(`doc-${i}.txt`));
    const items = await planFiles(many);
    expect(items.filter((item) => !item.skip)).toHaveLength(MAX_BATCH_FILES);
    expect(items.at(-1)?.skip).toMatch(/limit of 100/);

    const first = await planFiles(many.slice(0, MAX_BATCH_FILES - 1));
    const more = await planFiles([file("x.txt"), file("y.txt"), file("bad.xlsx")], first);
    expect(more.slice(-3).map((item) => item.skip)).toEqual([
      undefined,
      expect.stringMatching(/limit/),
      expect.stringMatching(/Unsupported/),
    ]);
  });

  it("gives every file its own idempotency key and keeps titles within the server's limit", async () => {
    const items = await planFiles([file(`${"long ".repeat(80)}.pdf`), file("b.pdf")]);
    expect(items[0].title.length).toBe(255);
    expect(items[0].key).toMatch(/^[0-9a-f]{32}$/);
    expect(items[0].key).not.toBe(items[1].key);
    expect(items[0].id).not.toBe(items[1].id);
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
