/**
 * The LLM gateway's endpoints: the model chain's state for system
 * administrators, a one-token probe per link, and what chat can do now.
 */

import { request } from "./client";
import type { AIProbeResult, AIStatus, ChatCapabilities } from "./types";

export const aiApi = {
  /** The chain and today's totals; cheap enough to poll while the panel is open. */
  status: () => request<AIStatus>("/api/v1/admin/ai/"),
  /** A real one-token request to one link (`provider:model`). Throttled and audited. */
  test: (link: string) =>
    request<AIProbeResult>("/api/v1/admin/ai/test/", { method: "POST", body: { link } }),
  /** Whether answers are being written or quoted right now, for anyone signed in. */
  capabilities: () => request<ChatCapabilities>("/api/v1/chat/capabilities/"),
};
