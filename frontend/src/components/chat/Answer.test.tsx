import { describe, expect, it } from "vitest";

import type { ChatMessage, ChatResponse, Citation } from "../../api/types";
import { answerAsMarkdown, fromMessage, fromResponse, type AnswerView } from "./Answer";

describe("a copied answer's sources", () => {
  const citation = (extra: Partial<Citation>): Citation => ({
    marker: 1,
    document_id: 3,
    document_title: "Syllabus",
    page_number: 5,
    chunk_id: 9,
    character_start: 0,
    character_end: 40,
    text: "Table 2, row 1: Course Code: 23CS101; Credits: 4",
    relevance: 0.9,
    ...extra,
  });
  const answer = (citations: Citation[]) =>
    ({ text: "It has 4 credits [1].", abstained: false, citations }) as unknown as AnswerView;

  it("name the table row a figure was taken from", () => {
    const table = {
      number: 2,
      first_row: 1,
      last_row: 1,
      rows: [1],
      narrowed: true,
      label: "Table 2, row 1",
    };
    expect(answerAsMarkdown(answer([citation({ table })]))).toContain(
      "[1] Syllabus, p. 5, Table 2, row 1",
    );
  });

  it("are a page alone for prose", () => {
    expect(answerAsMarkdown(answer([citation({})]))).toMatch(/\[1\] Syllabus, p\. 5$/);
  });
});

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
