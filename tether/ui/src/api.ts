import type {
  AuditLog,
  DeletionRequest,
  Lineage,
  Model,
  RagHit,
  Subject,
} from "./types";

const BASE = (import.meta.env.VITE_API_URL as string | undefined) ?? "/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      detail = (await resp.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new Error(`${resp.status}: ${typeof detail === "string" ? detail : JSON.stringify(detail)}`);
  }
  return (await resp.json()) as T;
}

const enc = encodeURIComponent;

export const api = {
  subjects: (q: string) => request<Subject[]>(`/subjects?q=${enc(q)}&limit=100`),
  subject: (id: string) => request<Subject>(`/subjects/${enc(id)}`),
  lineage: (id: string) => request<Lineage>(`/subjects/${enc(id)}/lineage`),
  setConsent: (id: string, consent_status: string) =>
    request(`/subjects/${enc(id)}/consent`, {
      method: "POST",
      body: JSON.stringify({ consent_status }),
    }),
  requestDeletion: (id: string, reason: string) =>
    request<DeletionRequest>(`/subjects/${enc(id)}/deletion-requests`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    }),
  deletion: (id: string) => request<DeletionRequest>(`/deletion-requests/${enc(id)}`),
  deletions: () => request<DeletionRequest[]>(`/deletion-requests`),
  retry: (id: string) =>
    request<DeletionRequest>(`/deletion-requests/${enc(id)}/retry`, { method: "POST" }),
  certificateDownloadUrl: (id: string) =>
    `${BASE}/deletion-requests/${enc(id)}/certificate?download=true`,
  models: () => request<Model[]>(`/models`),
  audit: () => request<AuditLog>(`/audit-log?limit=300`),
  rag: (query: string, top_k = 8) =>
    request<{ results: RagHit[] }>(`/rag/query`, {
      method: "POST",
      body: JSON.stringify({ query, top_k }),
    }),
};
