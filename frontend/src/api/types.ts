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
  | "INDEXING"
  /** Post-publish enrichment: the brief, related documents, thumbnail. */
  | "INSIGHTS";

/** Who decided a document's type: the upload default, the detector, a person. */
export type TypeSource = "default" | "auto" | "user";

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
  create_collections: boolean;
  /**
   * A one-click demo visitor: read-only, enforced by the server. Optional,
   * so an absent flag (an older server) means "not a guest".
   */
  is_guest?: boolean;
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
  /** Detail and Versions only: the uploader's name, or "". */
  uploaded_by_name?: string;
  created_at: string;
}

export interface DocumentSummary {
  id: number;
  title: string;
  document_type: DocumentType;
  /** Optional so older fixtures stay valid; the API always sends them. */
  document_type_source?: TypeSource;
  /** The detector's confidence when the source is "auto", else null. */
  type_confidence?: number | null;
  department: string;
  owner: number | null;
  owner_username: string;
  status: DocumentStatus;
  /** The stage an in-flight run is at; null once it has finished. */
  current_stage?: Stage | null;
  current_version: DocumentVersion | null;
  /** Pages of the live extraction pass; null before there is one. */
  page_count?: number | null;
  /** Whether GET .../thumbnail/ will answer with an image. */
  has_thumbnail?: boolean;
  is_archived: boolean;
  archived_at: string | null;
  /** Whether the caller may edit, archive or delete it - a UI hint only. */
  can_manage: boolean;
  created_at: string;
  updated_at: string;
}

export interface DocumentCounts {
  tables: number;
  figures: number;
  /** FOUND fields of the newest extraction under the current type. */
  fields_found: number;
}

export interface DocumentDetail extends DocumentSummary {
  counts?: DocumentCounts;
  /** Collections the caller can see that hold this document. */
  collections?: { id: number; name: string }[];
}

/** A readable document holding the same file, named in an upload's response. */
export interface DuplicateHint {
  id: number;
  title: string;
  status: DocumentStatus;
  is_archived: boolean;
  /** The matching file is that document's current version, not an older one. */
  is_current_version: boolean;
}

/** POST /documents/ - the new document, and what it may duplicate. */
export interface CreatedDocument extends DocumentDetail {
  possible_duplicates?: DuplicateHint[];
}

/** POST .../bulk-delete/ - ids sorted into what happened to each. */
export interface BulkDeleteResult {
  deleted: number[];
  /** Visible to the caller, but not theirs to delete. Absent for conversations. */
  forbidden?: number[];
  /** Gone already, or not visible to the caller - deliberately alike. */
  not_found: number[];
  /** Documents only: the delete itself failed (e.g. still being processed). */
  failed?: number[];
  /** With `all`: how many still match and may be deleted. Repeat until 0. */
  remaining?: number;
  /** With `all`: how many match that the caller may see but not delete. */
  skipped?: number;
  /** With `all`: the snapshot instant to send back with each next request. */
  as_of?: string;
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
  /**
   * "document_type": the product's own type, from the type detector.
   * "layout_class": a page-layout label (letter, memo...) - shown as a
   * layout, never as the document's type.
   */
  taxonomy?: "document_type" | "layout_class";
  predicted_label: string;
  confidence: number;
  /** The classifier declined to name a label; metadata.top_label is its guess. */
  abstained?: boolean;
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
  /** Retrieval's fused score. Results are ordered by `relevance` when set. */
  score: number;
  /** The cross-encoder's score for this passage; null when nothing reranked. */
  relevance: number | null;
  /** Only ever true when the request asked for passages below the floor. */
  below_floor: boolean;
  matched_by: string[];
  lexical_rank: number | null;
  vector_rank: number | null;
}

export interface SearchResponse {
  query: string;
  count: number;
  limit: number;
  max_limit: number;
  /** False means the vector half scored vocabulary overlap, not meaning. */
  semantic: boolean;
  embedding_model: string;
  retrieval: {
    lexical_candidates: number;
    vector_candidates: number;
    authorized_documents: number;
  };
  /** An empty `reranker` means results are in retrieval (fused) order. */
  ranking: {
    reranker: string;
    degraded: boolean;
    relevance_floor: number | null;
    reranked: number;
    /** Candidates scored below the floor and left out unless asked for. */
    below_floor: number;
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
  /** "documents": grounded and cited; "general": general knowledge, uncited; "none": abstained. */
  source?: AnswerSource;
  /** Who wrote it, for people: "Gemini 3.5 Flash", "Quoted from your documents"; "" when no answerer ran. */
  model_display?: string;
  /** What ran in a lesser mode for this answer. */
  degraded?: Degraded;
}

/** `reason`: "capacity" (comes back by itself) or "unavailable" (a failure). */
export interface Degraded {
  answerer: boolean;
  reranker: boolean;
  reason: "capacity" | "unavailable" | null;
}

export type AnswerSource = "documents" | "general" | "none";

export interface MessageMetadata {
  source?: AnswerSource;
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
  /** What the provider said about the answer (documents.rag.engine.llm_metadata). */
  llm?: Record<string, unknown>;
  degraded?: Degraded;
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
  model_display?: string;
  degraded?: Degraded | null;
  /** The reader's own rating of this answer, if they gave one. */
  my_feedback?: { rating: FeedbackRating; comment: string } | null;
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
  /** Sign-up requests waiting for this administrator; null for anyone else. */
  pending_signup_requests?: number | null;
  /** Trends for the home screen's charts (src/api/analytics.ts). */
  activity?: import("./analytics").DashboardActivity;
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
  /** Whether the caller may edit this account (the API's own rule). */
  can_manage: boolean;
}

export interface RoleOption {
  value: Role;
  label: string;
}

/** GET /api/v1/auth/signup/ - what the sign-up form may offer, if anything. */
export interface SignupOptions {
  enabled: boolean;
  departments: Department[];
  roles: RoleOption[];
  /** The only email domains a request may use (and their subdomains); empty for any. */
  email_domains?: string[];
}

export interface SignupInput {
  username: string;
  email: string;
  first_name: string;
  last_name: string;
  password: string;
  department: string;
  role: Role;
  note: string;
}

export type SignupStatus = "PENDING" | "APPROVED" | "REJECTED";

export interface SignupRequest {
  id: number;
  username: string;
  email: string;
  first_name: string;
  last_name: string;
  /** What the applicant asked for; approval decides what they get. */
  department: string;
  role: Role;
  note: string;
  status: SignupStatus;
  created_at: string;
  reviewed_by: string | null;
  reviewed_at: string | null;
  decision_note: string;
  /** The account approval created. */
  user: number | null;
  /**
   * Accounts the reviewer administers that already use this email address.
   * Nothing verifies the address, so this is the sign to look twice.
   */
  email_in_use?: string[];
}

/** POST /api/v1/admin/signup-requests/bulk-reject/ */
export interface BulkRejectResult {
  rejected: number[];
  /** Not the caller's to decide, or decided meanwhile - deliberately alike. */
  not_found: number[];
  /** With `all`: how many still match. Repeat, passing `as_of`, until 0. */
  remaining?: number;
  as_of?: string;
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
  /** Another department's document: neither its id nor its title is shown. */
  document_withheld: boolean;
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
    /** The model chain's live state (GET /api/v1/admin/ai/ returns the same). */
    enabled?: boolean;
    chain?: AILink[];
    totals?: AITotals;
  };
  pipeline: Record<string, boolean | number | null>;
  debug: boolean;
}

export interface CollectionMember {
  id: number;
  title: string;
}

export interface Collection {
  id: number;
  name: string;
  description: string;
  department: string;
  documents: CollectionMember[];
  document_count: number;
  created_by: string | null;
  can_manage: boolean;
  created_at: string;
  updated_at: string;
}

// --- The LLM gateway (System status → AI providers) ----------------------------

export type AILinkState = "ready" | "cooling" | "exhausted_today" | "unconfigured";

/** One provider:model link of the chain. Never a key or a URL; `key_env` is the variable's NAME. */
export interface AILink {
  id: string;
  provider: string;
  provider_display: string;
  model: string;
  label: string;
  state: AILinkState;
  cooling_until: string | null;
  calls_today: number;
  /** null: no daily limit is enforced for this link. */
  daily_limit: number | null;
  resets_at: string;
  last_error_type: string | null;
  last_error_status: number | null;
  last_error_at: string | null;
  last_success_at: string | null;
  trains_on_input: boolean;
  key_env: string;
}

export interface AITotals {
  /** null when a usable link has no daily limit. */
  capacity_left_today: number | null;
  calls_today: number;
  chat_reserve: number;
  resets_at: string | null;
  generative_available: boolean;
  resumes_at: string | null;
  answers_today?: {
    generative: number;
    quoted: number;
    abstained: number;
    quoted_ai_unavailable: number;
    since: string;
  };
}

export interface AIStatus {
  checked_at: string;
  enabled: boolean;
  chain: AILink[];
  totals: AITotals;
}

export interface AIProbeResult {
  link: string;
  ok: boolean;
  status_code: number | null;
  /** An exception class name, or "unconfigured" when the link has no key. */
  error_type: string | null;
  model: string;
  latency_ms: number;
  state: AILink;
}

export interface ChatCapabilities {
  generative_available: boolean;
  mode: "writing" | "quoting";
  resumes_at: string | null;
  general_knowledge: boolean;
}
