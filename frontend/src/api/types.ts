/**
 * Response shapes, mirroring the DRF serializers in backend/documents/api
 * and backend/documents/{search,chat}. Hand-written rather than generated,
 * so anything the backend adds is invisible here until it is read on
 * purpose - which is the point of a console used to check what the API
 * actually returns.
 */

export type DocumentStatus =
  | "UPLOADED"
  | "QUEUED"
  | "PROCESSING"
  | "COMPLETED"
  | "FAILED";

export type DocumentType =
  | "GENERAL"
  | "REGULATION"
  | "POLICY"
  | "SYLLABUS"
  | "CIRCULAR"
  | "FORM"
  | "OTHER";

export const DOCUMENT_TYPES: DocumentType[] = [
  "GENERAL",
  "REGULATION",
  "POLICY",
  "SYLLABUS",
  "CIRCULAR",
  "FORM",
  "OTHER",
];

export type JobStatus = "QUEUED" | "PROCESSING" | "COMPLETED" | "FAILED";

export type EventStatus = "STARTED" | "SUCCEEDED" | "SKIPPED" | "FAILED";

export type Stage =
  | "INGESTION"
  | "VALIDATION"
  | "TEXT_EXTRACTION"
  | "OCR"
  | "CLASSIFICATION"
  | "LAYOUT_ANALYSIS"
  | "EXTRACTION"
  | "INDEXING";

export type FieldStatus = "FOUND" | "NOT_FOUND" | "INVALID" | "CONFLICTED";

export interface Paginated<T> {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
}

export interface Me {
  id: number;
  username: string;
  email: string;
  is_superuser: boolean;
  role: string | null;
  department: string;
}

export interface DocumentVersion {
  id: number;
  version_number: number;
  file: string;
  file_name: string;
  file_size: number;
  mime_type: string;
  checksum: string;
  uploaded_by: number | null;
  created_at: string;
}

export interface DocumentSummary {
  id: number;
  title: string;
  document_type: DocumentType;
  department: string;
  owner: number | null;
  status: DocumentStatus;
  current_version: DocumentVersion | null;
  created_at: string;
  updated_at: string;
}

export interface ProcessingEvent {
  id: number;
  stage: Stage;
  status: EventStatus;
  message: string;
  metadata: Record<string, unknown>;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
}

export interface ProcessingJob {
  id: number;
  document_version: number;
  status: JobStatus;
  current_stage: Stage | "";
  attempt: number;
  error_code: string;
  error_message: string;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
  events: ProcessingEvent[];
}

export interface PageContent {
  page_number: number;
  text: string;
  extraction_method: string;
  character_count: number;
  metadata: Record<string, unknown>;
}

export interface DocumentContent {
  id: number;
  document_version: number;
  processing_job: number | null;
  processing_version: string;
  page_count: number;
  metadata: Record<string, unknown>;
  created_at: string;
  pages: PageContent[];
}

export interface ClassificationResult {
  id: number;
  document_content: number | null;
  processing_job: number | null;
  model_name: string;
  model_version: string;
  predicted_label: string;
  confidence: number;
  input_reference: string;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface ExtractedField {
  id: number;
  field_name: string;
  status: FieldStatus;
  value: string;
  confidence: number;
  page_number: number | null;
  character_start: number | null;
  character_end: number | null;
  matched_text: string;
  metadata: Record<string, unknown>;
}

export interface ExtractionResult {
  id: number;
  document_content: number | null;
  processing_job: number | null;
  model_name: string;
  model_version: string;
  schema_name: string;
  schema_version: string;
  document_type: string;
  field_count: number;
  found_count: number;
  input_reference: string;
  metadata: Record<string, unknown>;
  created_at: string;
  extracted_fields: ExtractedField[];
}

export interface ChunkEmbedding {
  id: number;
  model_name: string;
  model_version: string;
  dimensions: number;
  semantic: boolean;
  created_at: string;
}

export interface DocumentChunk {
  id: number;
  chunk_index: number;
  page_number: number;
  character_start: number;
  character_end: number;
  text: string;
  character_count: number;
  chunking_strategy: string;
  chunking_version: string;
  metadata: Record<string, unknown>;
  created_at: string;
  embeddings: ChunkEmbedding[];
}

export interface SearchHit {
  document_id: number;
  document_title: string;
  page_number: number;
  chunk_id: number;
  character_start: number;
  character_end: number;
  text: string;
  score: number;
  matched_by: string[];
  lexical_rank: number | null;
  vector_rank: number | null;
}

export interface SearchResponse {
  query: string;
  count: number;
  /** False means the vector half scored vocabulary overlap, not meaning. */
  semantic: boolean;
  embedding_model: string;
  retrieval: {
    lexical_candidates: number;
    vector_candidates: number;
    authorized_documents: number;
  };
  notes: string[];
  results: SearchHit[];
}

export interface Citation {
  marker: number;
  document_id: number;
  document_title: string;
  page_number: number;
  chunk_id: number;
  character_start: number;
  character_end: number;
  text: string;
  relevance: number;
}

export interface ChatResponse {
  conversation_id: number;
  question: string;
  answer: string;
  abstained: boolean;
  abstention_reason: string;
  citations: Citation[];
  grounding: {
    support_ratio: number;
    failures: string[];
  };
  retrieval: {
    retrieved: number;
    reranked: number;
    passages_used: number;
    semantic: boolean;
    reranker: string;
    suspected_injections: number;
  };
  model: {
    name: string;
    version: string;
    /** False means the answer quotes passages verbatim rather than writing. */
    generative: boolean;
  };
  duration_ms: number;
  notes: string[];
  message_id: number;
}

export interface Health {
  status: string;
  database: string;
}
