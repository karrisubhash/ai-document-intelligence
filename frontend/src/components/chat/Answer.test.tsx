import { describe, expect, it } from "vitest";

import type { ChatMessage, ChatResponse } from "../../api/types";
import { fromMessage, fromResponse } from "./Answer";

describe("the answer's author", () => {
  it("is the model's name, not the answerer's class", () => {
    const response = {
      model: { name: "openai-compatible", version: "groq/openai/gpt-oss-120b", generative: true },
      model_display: "GPT-OSS 120B · Groq",
      plan: { planned: false, steps: [], unanswered: [] },
      retrieval: { retrieved: 1, reranked: 1, passages_used: 1, semantic: false, reranker: "", suspected_injections: 0 },
      grounding: { support_ratio: 1, failures: [] },
    } as unknown as ChatResponse;
    expect(fromResponse(response).modelName).toBe("GPT-OSS 120B · Groq");
  });

  it("falls back to what an older server sent", () => {
    const message = {
      model_name: "extractive-answerer",
      metadata: {},
      citations: [],
    } as unknown as ChatMessage;
    expect(fromMessage(message).modelName).toBe("extractive-answerer");
    expect(fromMessage({ ...message, model_display: "Quoted from your documents" }).modelName).toBe(
      "Quoted from your documents",
    );
  });
});
