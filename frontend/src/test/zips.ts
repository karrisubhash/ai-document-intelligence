/**
 * Zips for tests: a byte-level builder for the cases no tool writes on
 * request, and small archives made by real tools, as base64. Web APIs only
 * (CompressionStream, DataView), as the type check has no Node types.
 */
import { crc32 } from "../lib/zip";

export const fromBase64 = (value: string) =>
  new Blob([Uint8Array.from(atob(value), (char) => char.charCodeAt(0))]);

/** A zip as a File, as a person would pick it. */
export const zipFile = (zip: Blob, name = "archive.zip") =>
  new File([zip], name, { type: "application/zip" });

async function deflateRaw(data: Uint8Array): Promise<Uint8Array> {
  const reader = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(data);
      controller.close();
    },
  })
    .pipeThrough(
      new CompressionStream("deflate-raw") as unknown as TransformStream<Uint8Array, Uint8Array>,
    )
    .getReader();
  const chunks: Uint8Array[] = [];
  for (let next = await reader.read(); !next.done; next = await reader.read()) {
    chunks.push(next.value);
  }
  return concat(chunks);
}

function concat(parts: Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((sum, part) => sum + part.length, 0));
  let at = 0;
  for (const part of parts) {
    out.set(part, at);
    at += part.length;
  }
  return out;
}

export interface Spec {
  name?: string;
  rawName?: Uint8Array;
  data?: string | Uint8Array;
  method?: number;
  flags?: number;
  /** Claim a size other than the data's - a lie a zip bomb tells. */
  declaredSize?: number;
  crc?: number;
  madeBy?: number;
  unixMode?: number;
  /** Move the sizes and offset into a ZIP64 extra field. */
  zip64?: boolean;
}

/** Little-endian fields into a fixed-size record. */
function record(size: number, fill: (view: DataView) => void): Uint8Array {
  const bytes = new Uint8Array(size);
  fill(new DataView(bytes.buffer));
  return bytes;
}

const u64 = (view: DataView, at: number, value: number) =>
  view.setBigUint64(at, BigInt(value), true);

/** A zip, byte by byte, deflated by the platform's CompressionStream. */
export async function buildZip(
  specs: Spec[],
  options: { comment?: string; zip64?: boolean; disk?: number; count?: number } = {},
): Promise<Blob> {
  const encoder = new TextEncoder();
  const parts: Uint8Array[] = [];
  const central: Uint8Array[] = [];
  let offset = 0;
  for (const spec of specs) {
    const data = spec.data instanceof Uint8Array ? spec.data : encoder.encode(spec.data ?? "");
    const method = spec.method ?? 8;
    const body = method === 8 ? await deflateRaw(data) : data;
    const name = spec.rawName ?? encoder.encode(spec.name ?? "");
    const crc = spec.crc ?? crc32(data);
    const size = spec.declaredSize ?? data.length;
    const flags = spec.flags ?? 0;

    parts.push(
      record(30, (v) => {
        v.setUint32(0, 0x04034b50, true);
        v.setUint16(4, 20, true);
        v.setUint16(6, flags, true);
        v.setUint16(8, method, true);
        v.setUint32(14, crc, true);
        v.setUint32(18, body.length, true);
        v.setUint32(22, size, true);
        v.setUint16(26, name.length, true);
      }),
      name,
      body,
    );

    let extra: Uint8Array = new Uint8Array();
    let [compressed, uncompressed, at] = [body.length, size, offset];
    if (spec.zip64) {
      extra = record(28, (v) => {
        v.setUint16(0, 0x0001, true);
        v.setUint16(2, 24, true);
        u64(v, 4, size);
        u64(v, 12, body.length);
        u64(v, 20, offset);
      });
      [compressed, uncompressed, at] = [0xffffffff, 0xffffffff, 0xffffffff];
    }
    central.push(
      record(46, (v) => {
        v.setUint32(0, 0x02014b50, true);
        v.setUint16(4, ((spec.madeBy ?? 0) << 8) | 20, true);
        v.setUint16(6, 20, true);
        v.setUint16(8, flags, true);
        v.setUint16(10, method, true);
        v.setUint32(16, crc, true);
        v.setUint32(20, compressed, true);
        v.setUint32(24, uncompressed, true);
        v.setUint16(28, name.length, true);
        v.setUint16(30, extra.length, true);
        v.setUint32(38, ((spec.unixMode ?? 0) << 16) >>> 0, true);
        v.setUint32(42, at, true);
      }),
      name,
      extra,
    );
    offset += 30 + name.length + body.length;
  }

  const directorySize = central.reduce((sum, part) => sum + part.length, 0);
  const count = options.count ?? specs.length;
  parts.push(...central);
  if (options.zip64) {
    parts.push(
      record(56, (v) => {
        v.setUint32(0, 0x06064b50, true);
        u64(v, 4, 44);
        v.setUint16(12, 45, true);
        v.setUint16(14, 45, true);
        u64(v, 24, count);
        u64(v, 32, count);
        u64(v, 40, directorySize);
        u64(v, 48, offset);
      }),
      record(20, (v) => {
        v.setUint32(0, 0x07064b50, true);
        u64(v, 8, offset + directorySize);
        v.setUint32(16, 1, true);
      }),
    );
  }
  const comment = encoder.encode(options.comment ?? "");
  parts.push(
    record(22, (v) => {
      v.setUint32(0, 0x06054b50, true);
      v.setUint16(4, options.disk ?? 0, true);
      v.setUint16(8, options.zip64 ? 0xffff : count, true);
      v.setUint16(10, options.zip64 ? 0xffff : count, true);
      v.setUint32(12, options.zip64 ? 0xffffffff : directorySize, true);
      v.setUint32(16, options.zip64 ? 0xffffffff : offset, true);
      v.setUint16(20, comment.length, true);
    }),
    comment,
  );
  return new Blob([concat(parts) as BlobPart]);
}

// Generated once with the tools named; lib/zip.test.ts says what each holds.
// Python 3.13 zipfile:
export const PYTHON_ZIP =
  "UEsDBBQAAAAAAAAAIQAAAAAAAAAAAAAAAAAIAAAAcmVwb3J0cy9QSwMEFAAAAAgArUBYXaRdz5Q7AAAArAMAABYAAAByZXBvcnRzL2V4YW1fcnVsZXMudHh0c61IzFUoKs1JLbZSMDdVKEgtSk7NK1FILClJzUtJzEtOVcgsVihKLSzNLEpN0eNyHVU+qnxU+cAoBwBQSwMEFAAAAAAArUBYXWk1JoBIAAAASAAAAAgAAABzY2FuLnBuZ4lQTkcNChoKAAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8gISIjJCUmJygpKissLS4vMDEyMzQ1Njc4OTo7PD0+P1BLAwQUAAAICACtQFhd6BLIXRMAAAAQAAAADAAAAHLDqXN1bcOpLnR4dDs8J+/w+uTD21IOr1TIS8xNBQBQSwMEFAAAAAAArUBYXTmc+wYEAAAABAAAABMAAABfX01BQ09TWC8uX3NjYW4ucG5nanVua1BLAwQtAAAAAAAAACEAr0U3yP//////////DAAUAGZvcmNlZDY0LnR4dAEAEAC+AAAAAAAAAL4AAAAAAAAAemlwNjQgbG9jYWwgaGVhZGVyCnppcDY0IGxvY2FsIGhlYWRlcgp6aXA2NCBsb2NhbCBoZWFkZXIKemlwNjQgbG9jYWwgaGVhZGVyCnppcDY0IGxvY2FsIGhlYWRlcgp6aXA2NCBsb2NhbCBoZWFkZXIKemlwNjQgbG9jYWwgaGVhZGVyCnppcDY0IGxvY2FsIGhlYWRlcgp6aXA2NCBsb2NhbCBoZWFkZXIKemlwNjQgbG9jYWwgaGVhZGVyClBLAwQUAAAAAACtQFhdE/PlNhgAAAAYAAAACgAAAHNoZWV0Lnhsc3hub3QgcmVhbGx5IGEgc3ByZWFkc2hlZXRQSwECFAAUAAAAAAAAACEAAAAAAAAAAAAAAAAACAAAAAAAAAAAAAAAgAEAAAAAcmVwb3J0cy9QSwECFAAUAAAACACtQFhdpF3PlDsAAACsAwAAFgAAAAAAAAAAAAAAgAEmAAAAcmVwb3J0cy9leGFtX3J1bGVzLnR4dFBLAQIUABQAAAAAAK1AWF1pNSaASAAAAEgAAAAIAAAAAAAAAAAAAACAAZUAAABzY2FuLnBuZ1BLAQIUABQAAAgIAK1AWF3oEshdEwAAABAAAAAMAAAAAAAAAAAAAACAAQMBAAByw6lzdW3DqS50eHRQSwECFAAUAAAAAACtQFhdOZz7BgQAAAAEAAAAEwAAAAAAAAAAAAAAgAFAAQAAX19NQUNPU1gvLl9zY2FuLnBuZ1BLAQItAC0AAAAAAAAAIQCvRTfIvgAAAL4AAAAMAAAAAAAAAAAAAACAAXUBAABmb3JjZWQ2NC50eHRQSwECFAAUAAAAAACtQFhdE/PlNhgAAAAYAAAACgAAAAAAAAAAAAAAgAFxAgAAc2hlZXQueGxzeFBLBQYAAAAABwAHAJ0BAACxAgAAFgBtYWRlIGJ5IHB5dGhvbiB6aXBmaWxl";
// Python 3.13 zipfile writing to an unseekable stream:
export const PYTHON_STREAMED_ZIP =
  "UEsDBBQACAAIAAAAIQAAAAAAAAAAAAAAAAAMAAAAc3RyZWFtZWQudHh0Ky4pSk3MTU1RKM8syVBIVEhJLAESqcXJRZkFJflFXMWj8qPyo/KDVh4AUEsHCI3CFG4qAAAAwAMAAFBLAQIUABQACAAIAAAAIQCNwhRuKgAAAMADAAAMAAAAAAAAAAAAAACAAQAAAABzdHJlYW1lZC50eHRQSwUGAAAAAAEAAQA6AAAAZAAAAAAA";
// Windows PowerShell 5.1 Compress-Archive:
export const POWERSHELL_ZIP =
  "UEsDBBQAAAAIALJAWF3uNZX4JQAAACwBAAAPAAAAc3ViXG1pbnV0ZXMudHh0883MKy1JLVbIT1MoyUhVSMpPLEpRyE1NLcnMS9fj8h2VRZIFAFBLAwQUAAAACACyQFhd8rWqDB0AAADwAAAACgAAAG5vdGljZS50eHTzyUwqSiyqVEjOyS9OLVZILFGwUCjI1ePyGabiAFBLAQIUABQAAAAIALJAWF3uNZX4JQAAACwBAAAPAAAAAAAAAAAAAAAAAAAAAABzdWJcbWludXRlcy50eHRQSwECFAAUAAAACACyQFhd8rWqDB0AAADwAAAACgAAAAAAAAAAAAAAAABSAAAAbm90aWNlLnR4dFBLBQYAAAAAAgACAHUAAACXAAAAAAA=";
// Windows 11 tar.exe (bsdtar 3.7) with -a:
export const BSDTAR_ZIP =
  "UEsDBBQACAAIALJAWF0AAAAAAAAAAAAAAAAKACAAbm90aWNlLnR4dHV4CwABBAAAAAAEAAAAAFVUDQAHeBncankZ3Gp4Gdxq88lMKkosqlRIzskvTi1WSCxRsFAoyNXj8hmm4gBQSwcI8rWqDB0AAADwAAAAUEsDBBQAAAAAALJAWF0AAAAAAAAAAAAAAAAEACAAc3ViL3V4CwABBAAAAAAEAAAAAFVUDQAHeBncankZ3Gp4GdxqUEsDBBQACAAIALJAWF0AAAAAAAAAAAAAAAAPACAAc3ViL21pbnV0ZXMudHh0dXgLAAEEAAAAAAQAAAAAVVQNAAd4GdxqeRncangZ3GrzzcwrLUktVshPUyjJSFVIyk8sSlHITU0tycxL1+PyHZVFkgUAUEsHCO41lfglAAAALAEAAFBLAQIUAxQACAAIALJAWF3ytaoMHQAAAPAAAAAKABgAAAAAAAAAAAC2gQAAAABub3RpY2UudHh0dXgLAAEEAAAAAAQAAAAAVVQFAAF4GdxqUEsBAhQDFAAAAAAAskBYXQAAAAAAAAAAAAAAAAQAGAAAAAAAAAAAAP9BdQAAAHN1Yi91eAsAAQQAAAAABAAAAABVVAUAAXgZ3GpQSwECFAMUAAgACACyQFhd7jWV+CUAAAAsAQAADwAYAAAAAAAAAAAAtoG3AAAAc3ViL21pbnV0ZXMudHh0dXgLAAEEAAAAAAQAAAAAVVQFAAF4GdxqUEsFBgAAAAADAAMA7wAAADkBAAAAAA==";
// Windows 11 Explorer, Send to > Compressed (zipped) folder: "Café" in code page 437, no UTF-8 flag.
export const EXPLORER_ZIP =
  "UEsDBBQAAAAIAFJDWF2NwJiFIAAAANgAAAANAAAAQ2FmgiBtZW51LnR4dPMpzUvOUMgsVihOLSpLTVFIK8rPVcjLz8/T4/IZylIAUEsDBBQAAAAIAFJDWF2pk1s5HAAAALgAAAARAAAATWludXRlcy9ib2FyZC50eHRzyk8sSlHITU0tycxLV8jNzCstSS3W43Ia7MIAUEsBAhQAFAAAAAgAUkNYXY3AmIUgAAAA2AAAAA0AAAAAAAAAAQAgAAAAAAAAAENhZoIgbWVudS50eHRQSwECFAAUAAAACABSQ1hdqZNbORwAAAC4AAAAEQAAAAAAAAABACAAAABLAAAATWludXRlcy9ib2FyZC50eHRQSwUGAAAAAAIAAgB6AAAAlgAAAAAA";
