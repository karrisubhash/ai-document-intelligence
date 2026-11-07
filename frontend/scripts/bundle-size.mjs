// Bundle sizes after `npx vite build`: every JavaScript and CSS asset, raw
// and gzipped (what nginx sends), and the total a first visit downloads -
// the files dist/index.html loads itself - against a budget.
//
//   npm run size                 report; exit 1 if the first load is over budget
//   npm run size -- --budget=200 another budget, in kB gzipped
//
// Node's own zlib, no dependencies. Level 5, as nginx.conf compresses.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";

const DIST = new URL("../dist/", import.meta.url);
const budgetArg = process.argv.find((arg) => arg.startsWith("--budget="));
// The first load today is about 130 kB gzipped; the budget leaves room to
// grow without letting a heavy library slip into every page unnoticed.
const BUDGET_KB = budgetArg ? Number(budgetArg.slice("--budget=".length)) : 180;

let html;
try {
  html = readFileSync(new URL("index.html", DIST), "utf8");
} catch {
  console.error("dist/index.html not found: run `npx vite build` first.");
  process.exit(2);
}

const initial = new Set(
  [...html.matchAll(/(?:src|href)="\/(assets\/[^"]+\.(?:js|css))"/g)].map((match) => match[1]),
);

const assets = readdirSync(new URL("assets/", DIST))
  .filter((name) => /\.(js|css)$/.test(name))
  .map((name) => {
    const path = join(fileURLToPath(DIST), "assets", name);
    const raw = statSync(path).size;
    const gzip = gzipSync(readFileSync(path), { level: 5 }).length;
    return { name: `assets/${name}`, raw, gzip, first: initial.has(`assets/${name}`) };
  })
  .sort((a, b) => b.gzip - a.gzip);

const kb = (bytes) => (bytes / 1024).toFixed(1).padStart(7);
console.log("     raw kB   gzip kB  file");
for (const asset of assets) {
  console.log(`${kb(asset.raw)}   ${kb(asset.gzip)}  ${asset.name}${asset.first ? "  (first load)" : ""}`);
}

const firstLoad = assets.filter((asset) => asset.first).reduce((sum, asset) => sum + asset.gzip, 0);
const everything = assets.reduce((sum, asset) => sum + asset.gzip, 0);
console.log(`\nFirst load: ${(firstLoad / 1024).toFixed(1)} kB gzipped (budget ${BUDGET_KB} kB)`);
console.log(`Every screen: ${(everything / 1024).toFixed(1)} kB gzipped`);
if (firstLoad / 1024 > BUDGET_KB) {
  console.error(`Over budget by ${(firstLoad / 1024 - BUDGET_KB).toFixed(1)} kB.`);
  process.exit(1);
}
