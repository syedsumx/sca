export interface Deprecation {
  method: string;
  path: string;
  deprecation: string;
  sunset: string | null;
  successor: string | null;
}

export interface CairnClientOptions {
  /** Cairn's API, e.g. https://cairn.example.com/cairn/api */
  baseUrl: string;
  /** A Cairn access token or agent key, sent as a bearer token. */
  token?: string;
  fetch?: typeof fetch;
  /** How many times a 429/503 with Retry-After is retried (default 2). */
  retries?: number;
  timeoutMs?: number;
  onDeprecation?: (d: Deprecation) => void;
  headers?: Record<string, string>;
}

export type Query = Record<string, string | number | boolean | undefined | null>;

export class CairnError extends Error {
  readonly status: number;
  readonly detail: string;
  readonly body: unknown;
  constructor(status: number, detail: string, body: unknown);
}

export interface SqlResult {
  columns: string[];
  rows: unknown[][];
  [k: string]: unknown;
}

export class CairnClient {
  constructor(options: CairnClientOptions);
  readonly baseUrl: string;
  request<T = any>(method: string, path: string, opts?: { query?: Query; body?: unknown; headers?: Record<string, string> }): Promise<T>;
  me(): Promise<{ urn: string; [k: string]: unknown }>;
  deprecations(): Promise<{ min_notice_days: number; routes: Array<Record<string, string | null>> }>;
  datasetHealth(datasetUrn: string): Promise<any>;
  resolveDataset(name: string): Promise<any>;
  semanticSearch(query: string, opts?: { limit?: number; entityTypes?: string[] }): Promise<any>;
  ask(question: string): Promise<any>;
  chat(message: string, conversationId?: string): Promise<any>;
  runSql(connection: string, sql: string, opts?: { limit?: number }): Promise<SqlResult>;
  savedQueries(): Promise<any[]>;
  products(opts?: { q?: string; domain?: string; certified?: boolean }): Promise<any>;
  product(urn: string): Promise<any>;
  requestAccess(req: { productUrn: string; purpose: string; justification?: string; durationDays?: number }): Promise<any>;
  accessRequests(): Promise<any>;
  accessRequest(id: string): Promise<any>;
  customAgents(): Promise<any[]>;
  runAgent(name: string, input: string): Promise<{ run: string; agent: string; status: string; answer: string | null; calls: any[] }>;
}

export default CairnClient;
