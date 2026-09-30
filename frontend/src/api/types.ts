/**
 * Response shapes, mirroring the DRF serializers in backend/. Hand-written:
 * a field the backend adds is invisible here until someone reads it on
 * purpose, which keeps the interface from depending on anything by accident.
 */

export type DocumentStatus =
  | "UPLOADED"
  | "QUEUED"
  | "PROCESSING"
  | "COMPLETED"
  | "FAILED"
  | "QUARANTINED";

export const DOCUMENT_STATUSES: DocumentStatus[] = [
  "UPLOADED",
  "QUEUED",
  "PROCESSING",
  "COMPLETED",
  "FAILED",
  "QUARANTINED",
];

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

export type Role =
  | "SYSTEM_ADMIN"
  | "DEPARTMENT_ADMIN"
  | "FACULTY"
  | "STAFF"
  | "STUDENT";

export type JobStatus = "QUEUED" | "PROCESSING" | "COMPLETED" | "FAILED";

export type EventStatus = "STARTED" | "SUCCEEDED" | "SKIPPED" | "FAILED";

export type Stage =
  | "INGESTION"
  | "SCANNING"
  | "VALIDATION"
  | "TEXT_EXTRACTION"
  | "OCR"
  | "LAYOUT_ANALYSIS"
  | "CLASSIFICATION"
  | "EXTRACTION"
  | "INDEXING";

export type FieldStatus = "FOUND" | "NOT_FOUND" | "INVALID" | "CONFLICTED";

export interface Paginated<T> {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
}

export interface Capabilities {
  administer_users: boolean;
  administer_departments: boolean;
  view_audit_log: boolean;
  view_operations: boolean;
  view_system_status: boolean;
  upload_to_any_department: boolean;
}

export interface Me {
  id: number;
  username: string;
  email: string;
  first_name: string;
  last_name: string;
  is_superuser: boolean;
  role: Role | null;
  department: string;
  capabilities: Capabilities;
}

export interface DocumentVersion {
  id: number;
  version_number: number;
  file?: string;
  file_name: string;
  file_size: number;
  mime_type: string;
  checksum?: string;
  uploaded_by: number | null;
  created_at: string;
}

export interface DocumentSummary {
  id: number;
  title: string;
  document_type: DocumentType;
  department: string;
  owner: number | null;
  owner_username: string;
  status: DocumentStatus;
  current_version: DocumentVersion | null;
  is_archived: boolean;
  archived_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface DocumentDetail extends DocumentSummary {
  can_manage: boolean;
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
  model_name: string;
  model_version: string;
  predicted_label: string;
  confidence: number;
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
  model_name: string;
  model_version: string;
  schema_name: string;
  schema_version: string;
  document_type: string;
  field_count: number;
  found_count: number;
  metadata: Record<string, unknown>;
  created_at: string;
  extracted_fields: ExtractedField[];
}

export interface DocumentTable {
  id: number;
  table_index: number;
  page_number: number;
  character_start: number;
  character_end: number;
  header: string[];
  rows: string[][];
  row_count: number;
  column_count: number;
  detector: string;
  detector_version: string;
}

export interface DocumentFigure {
  id: number;
  figure_index: number;
  page_number: number;
  width: number;
  height: number;
  description: string;
  description_source: string;
  detector: string;
  detector_version: string;
}

export interface SourcePassage {
  document_id: number;
  document_title: string;
  chunk_id: number;
  page_number: number;
  page_count: number;
  character_start: number;
  character_end: number;
  chunk_text: string;
  page_text: string;
  extraction_method: string;
  version_number: number;
  /** False when the document has been reprocessed or re-versioned since. */
  is_current: boolean;
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

export interface PlanStep {
  question: string;
  abstained: boolean;
  abstention_reason: string;
}

export interface ChatResponse {
  conversation_id: number;
  question: string;
  resolved_question: string;
  answer: string;
  abstained: boolean;
  abstention_reason: string;
  citations: Citation[];
  grounding: { support_ratio: number; failures: string[] };
  retrieval: {
    retrieved: number;
    reranked: number;
    passages_used: number;
    semantic: boolean;
    reranker: string;
    suspected_injections: number;
  };
  model: { name: string; version: string; generative: boolean };
  plan: { planned: boolean; steps: PlanStep[]; unanswered: string[] };
  duration_ms: number;
  timings: Record<string, number>;
  notes: string[];
  message_id: number;
}

export interface MessageMetadata {
  reranker?: string;
  semantic_retrieval?: boolean;
  suspected_injections?: number;
  grounding_failures?: string[];
  notes?: string[];
  planned?: boolean;
  plan?: string[];
  unanswered?: string[];
  followup?: {
    rewritten: boolean;
    resolved_question: string;
    borrowed_terms: string[];
  };
  timings?: Record<string, number>;
}

export interface ChatMessage {
  id: number;
  role: "USER" | "ASSISTANT";
  content: string;
  abstained: boolean;
  abstention_reason: string;
  citations: Citation[];
  support_ratio: number;
  model_name: string;
  model_version: string;
  generative: boolean;
  retrieved: number;
  passages_used: number;
  duration_ms: number;
  metadata: MessageMetadata;
  created_at: string;
}

export interface ConversationSummary {
  id: number;
  title: string;
  department: string;
  created_at: string;
  updated_at: string;
  message_count: number;
}

export interface Conversation {
  id: number;
  title: string;
  department: string;
  created_at: string;
  updated_at: string;
  messages: ChatMessage[];
}

export type FeedbackRating = "HELPFUL" | "NOT_HELPFUL" | "INCORRECT";

export interface DashboardDocument {
  id: number;
  title: string;
  status: DocumentStatus;
  document_type: DocumentType;
  department: string;
  updated_at: string;
  file_name: string;
}

export interface Dashboard {
  documents: { total: number; by_status: Partial<Record<DocumentStatus, number>> };
  recent_documents: DashboardDocument[];
  needs_attention: DashboardDocument[];
  recent_conversations: {
    id: number;
    title: string;
    updated_at: string;
    message_count: number;
  }[];
}

export interface Department {
  id: number;
  name: string;
  code: string;
}

export interface AdminDepartment extends Department {
  description: string;
  is_active: boolean;
  member_count: number | null;
  document_count: number | null;
  created_at: string;
  updated_at: string;
}

export interface AdminUser {
  id: number;
  username: string;
  email: string;
  first_name: string;
  last_name: string;
  is_active: boolean;
  is_superuser: boolean;
  role: Role;
  department: string;
  last_login: string | null;
  date_joined: string;
}

export interface RoleOption {
  value: Role;
  label: string;
}

export interface AuditEntry {
  id: number;
  created_at: string;
  action: string;
  result: "ALLOWED" | "DENIED";
  user: number | null;
  username: string;
  document: number | null;
  document_title: string;
  metadata: Record<string, unknown>;
}

export interface OperationsJob {
  id: number;
  document_id: number;
  document_title: string;
  department: string;
  version_number: number;
  is_current_version: boolean;
  status: JobStatus;
  current_stage: Stage | "";
  attempt: number;
  error_code: string;
  error_message: string;
  is_stalled: boolean;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ComponentHealth {
  status: "ok" | "failed" | "skipped";
  latency_ms: number;
  error?: string;
  detail?: string;
}

export interface SystemStatus {
  checked_at: string;
  components: Record<string, ComponentHealth>;
  queues: Record<string, number>;
  documents: { by_status: Record<string, number>; archived: number };
  jobs_last_24h: {
    by_status: Record<string, number>;
    timeouts: number;
    stalled_now: number;
  };
  users: { active: number; inactive: number };
  ai: {
    embedding: {
      provider: string;
      version?: string;
      dimensions?: number;
      semantic: boolean;
    };
    reranker: { name: string; version: string; semantic: boolean };
    answerer: { name: string; version: string; generative: boolean };
    chunking: { strategy: string; version: string; size: number; overlap: number };
    retrieval: Record<string, number | null>;
  };
  pipeline: Record<string, boolean | number | null>;
  debug: boolean;
}
