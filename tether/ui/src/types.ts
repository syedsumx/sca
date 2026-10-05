export type NodeType =
  | "subject"
  | "source_record"
  | "chunk"
  | "vector"
  | "collection"
  | "snapshot"
  | "model"
  | "memory";

export interface Subject {
  subject_id: string;
  display_name: string | null;
  email: string | null;
  consent_status: string | null;
  license: string | null;
  retention_until: string | null;
  restrictions: string[];
  artifact_count: number;
  deletion_request_id: string | null;
  deletion_status: string | null;
}

export interface LineageNode {
  id: string;
  type: NodeType;
  ref: string;
  status: string;
  subject_ids: string[];
  policy: Record<string, unknown>;
  attrs: Record<string, unknown>;
}

export interface LineageEdge {
  source: string;
  target: string;
  type: string;
}

export interface Lineage {
  subject_id: string;
  nodes: LineageNode[];
  edges: LineageEdge[];
}

export interface Certificate {
  certificate_id: string;
  request_id: string;
  subject_id: string;
  issued_at: string;
  sha256: string;
  verified: boolean;
  content: Record<string, unknown> & {
    purged?: { counts?: Record<string, number>; rows_deleted?: number };
    models_flagged_for_retraining?: { model: string; reason: string }[];
    memory_needing_regeneration?: string[];
  };
}

export interface HistoryEntry {
  status: string;
  at: string;
  [key: string]: unknown;
}

export interface DeletionRequest {
  id: string;
  subject_id: string;
  status: string;
  reason: string | null;
  created: boolean;
  error: Record<string, unknown> | null;
  history: HistoryEntry[];
  inventory: { counts?: Record<string, number>; models?: string[] };
  attempts: number;
  created_at: string;
  completed_at: string | null;
  certificate: Certificate | null;
}

export interface Model {
  id: string;
  name: string;
  version: string;
  trained_on: string[];
  status: string;
  needs_retraining: boolean;
  retraining_reasons: { request_id?: string; subject_id?: string; reason: string; flagged_at?: string }[];
  created_at: string;
}

export interface AuditEntry {
  id: number;
  ts: string;
  event_type: string;
  subject_id: string | null;
  entity_id: string | null;
  payload: Record<string, unknown>;
  hash: string;
}

export interface AuditLog {
  entries: AuditEntry[];
  chain_ok: boolean;
  chain_entries: number;
  first_broken_id: number | null;
}

export interface RagHit {
  vector_id: string;
  collection: string;
  chunk_id: string | null;
  content: string;
  score: number;
  subject_ids: string[];
}
