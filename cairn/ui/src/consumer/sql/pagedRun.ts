/**
 * A statement on Trino read page by page (L163): started as the person (one entry in their history), its pages read
 * until it ends or ``limit`` rows have come, with what Trino reports along the way. Stop (``signal``) cancels the
 * query in Trino itself, so it stops using the cluster, not only the page waiting for it.
 */
import type { ConsumerClient } from '../api/client';
import type { DataResult, StatementPage } from '../api/types';

export interface PagedProgress {
    rows: number;
    state?: string | null;
    stats: StatementPage['stats'];
    id: string | null;
}

export class StatementStopped extends Error {
    constructor() {
        super('Stopped');
        this.name = 'StatementStopped';
    }
}

export async function runPaged(client: Pick<ConsumerClient, 'startStatement' | 'nextStatement' | 'cancelStatement'>,
    req: { sql: string; connection: string; warehouse?: string; limit: number },
    signal: AbortSignal, onProgress?: (p: PagedProgress) => void): Promise<DataResult> {
    const started = Date.now();
    const rows: unknown[][] = [];
    let columns: { name: string; type: string }[] | null = null;
    const masked = new Set<string>();
    let cursor: string | null = null;
    const stop = async () => {
        if (cursor) {
            const c = cursor;
            cursor = null;
            await client.cancelStatement(c).catch(() => undefined);    // ended already: nothing to stop
        }
        throw new StatementStopped();
    };
    if (signal.aborted) throw new StatementStopped();
    // Reading more than ``limit`` is never asked for: Trino stops at it.
    let page: StatementPage = await client.startStatement({ sql: req.sql, connection: req.connection, warehouse: req.warehouse,
        limit: req.limit, history: true });
    for (;;) {
        columns = page.columns ?? columns;
        rows.push(...page.rows);
        page.masked_columns.forEach((c) => masked.add(c));
        cursor = page.next;
        onProgress?.({ rows: rows.length, state: page.state, stats: page.stats, id: page.id });
        if (!cursor) break;
        if (signal.aborted) await stop();
        try {
            // Not aborted with the signal: the request finishes and the query is cancelled with its cursor.
            page = await client.nextStatement(cursor);
        } catch (e) {
            if (signal.aborted) await stop();
            throw e;
        }
        if (signal.aborted) {
            cursor = page.next;                         // the newest place in the query: cancel it there
            await stop();
        }
    }
    return {
        columns: (columns ?? []).map((c) => c.name),
        types: (columns ?? []).map((c) => c.type),
        rows: rows.slice(0, req.limit),
        masked_columns: [...masked],
        truncated: rows.length >= req.limit,
        elapsed_ms: page.stats.elapsedTimeMillis ?? Date.now() - started,
        tables: [],
        query_id: page.id,
        stats: page.stats,
        ...(req.warehouse ? { warehouse: req.warehouse } : {}),
    };
}
