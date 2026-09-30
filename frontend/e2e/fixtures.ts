import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));

/** A document from the governed, synthetic evaluation corpus. */
export function corpusFile(slug: string) {
  const path = join(here, "..", "..", "evaluation", "corpus", `${slug}.txt`);
  return { name: `${slug}.txt`, mimeType: "text/plain", buffer: readFileSync(path) };
}

/**
 * A small valid PDF with a real text layer, one line per text operator, so
 * the PDF extraction and preview paths run on something that looks like a
 * document rather than a single string.
 */
export function makePdf(pages: string[][]): Buffer {
  const objects: string[] = [];
  const pageIds = pages.map((_, index) => 4 + index * 2);
  objects.push("<< /Type /Catalog /Pages 2 0 R >>");
  objects.push(
    `<< /Type /Pages /Kids [${pageIds.map((id) => `${id} 0 R`).join(" ")}] /Count ${pages.length} >>`,
  );
  objects.push("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>");

  pages.forEach((lines, index) => {
    const contentId = 5 + index * 2;
    objects.push(
      `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents ${contentId} 0 R >>`,
    );
    const escaped = lines.map((line) =>
      line.replace(/\\/g, "\\\\").replace(/\(/g, "\\(").replace(/\)/g, "\\)"),
    );
    const body = [
      "BT /F1 12 Tf 16 TL 72 720 Td",
      ...escaped.map((line) => `(${line}) Tj T*`),
      "ET",
    ].join("\n");
    objects.push(`<< /Length ${Buffer.byteLength(body)} >>\nstream\n${body}\nendstream`);
  });

  let pdf = "%PDF-1.4\n";
  const offsets: number[] = [];
  objects.forEach((object, index) => {
    offsets.push(Buffer.byteLength(pdf));
    pdf += `${index + 1} 0 obj\n${object}\nendobj\n`;
  });
  const xref = Buffer.byteLength(pdf);
  pdf += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  for (const offset of offsets) pdf += `${String(offset).padStart(10, "0")} 00000 n \n`;
  pdf += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(pdf, "latin1");
}

export const PASSWORD = process.env.E2E_PASSWORD ?? "";
