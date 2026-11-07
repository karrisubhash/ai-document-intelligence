/**
 * The front door's API: the one-click demo and the sample library.
 *
 * GET  /api/v1/auth/demo/     -> {enabled}
 * POST /api/v1/auth/demo/     -> 201, the same body as a sign-in (a fresh,
 *                                read-only guest, signed in with a session)
 * POST /api/v1/admin/samples/ -> 202 {queued, skipped} (system administrators)
 */

import { request } from "./client";
import type { Me } from "./types";

export interface DemoStatus {
  /** On, and its department has something ready to read. */
  enabled: boolean;
}

export interface SampleLoadResult {
  /** Samples created and queued for processing. */
  queued: number;
  /** Samples the department already held (by checksum). */
  skipped: number;
}

export const demoApi = {
  status: () => request<DemoStatus>("/api/v1/auth/demo/", { quiet: true }),
  start: () => request<Me>("/api/v1/auth/demo/", { method: "POST", quiet: true }),
  /** Fill a department with the sample library - for an empty installation. */
  loadSamples: (department: string) =>
    request<SampleLoadResult>("/api/v1/admin/samples/", {
      method: "POST",
      body: { department },
    }),
};
