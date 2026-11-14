/**
 * SQL autocomplete for the SQL editor: what may come next at the cursor, from the tables the person may
 * query (GET /sql/schema already leaves out the rest), the query's own aliases, keywords and functions.
 *
 * - After FROM, JOIN, INTO, UPDATE or TABLE: tables (and schemas).
 * - After `name.`: that schema's tables, or the columns of the table or alias `name`.
 * - Elsewhere: the columns of the tables the query reads (with their table or alias when ambiguous),
 *   then keywords and functions.
 * Nothing is offered inside a string or a comment. Names that are not plain identifiers are quoted.
 */
import type { SchemaTable } from '../api/types';

export type Kind = 'table' | 'schema' | 'column' | 'keyword' | 'function';

export interface Suggestion {
    label: string;
    insert: string;
    kind: Kind;
    detail?: string;
}

export interface Completion {
    from: number; // where the replaced word starts
    to: number; // the cursor
    items: Suggestion[];
}

export const KEYWORDS = [
    'SELECT', 'FROM', 'WHERE', 'GROUP BY', 'ORDER BY', 'HAVING', 'LIMIT', 'OFFSET', 'JOIN', 'LEFT JOIN', 'RIGHT JOIN',
    'FULL JOIN', 'CROSS JOIN', 'INNER JOIN', 'ON', 'USING', 'AS', 'AND', 'OR', 'NOT', 'IN', 'IS', 'NULL', 'LIKE',
    'BETWEEN', 'EXISTS', 'CASE', 'WHEN', 'THEN', 'ELSE', 'END', 'DISTINCT', 'UNION', 'UNION ALL', 'INTERSECT', 'EXCEPT',
    'WITH', 'ASC', 'DESC', 'NULLS FIRST', 'NULLS LAST', 'CAST', 'TRY_CAST', 'OVER', 'PARTITION BY', 'ROWS', 'TRUE',
    'FALSE', 'FOR VERSION AS OF', 'FOR TIMESTAMP AS OF', 'DESCRIBE', 'SHOW', 'EXPLAIN',
];

export const FUNCTIONS = [
    'count', 'sum', 'avg', 'min', 'max', 'approx_distinct', 'approx_percentile', 'array_agg', 'map_agg', 'stddev',
    'variance', 'count_if', 'bool_and', 'bool_or', 'arbitrary', 'max_by', 'min_by', 'coalesce', 'nullif', 'if',
    'greatest', 'least', 'abs', 'round', 'ceil', 'floor', 'sqrt', 'power', 'mod', 'ln', 'log10', 'random', 'lower',
    'upper', 'trim', 'ltrim', 'rtrim', 'length', 'substr', 'replace', 'concat', 'split', 'split_part', 'strpos',
    'regexp_like', 'regexp_extract', 'regexp_replace', 'lpad', 'rpad', 'format', 'to_hex', 'sha256', 'md5',
    'json_extract', 'json_extract_scalar', 'json_parse', 'json_format', 'date_trunc', 'date_add', 'date_diff',
    'date_format', 'date_parse', 'from_unixtime', 'to_unixtime', 'current_date', 'current_timestamp', 'now', 'year',
    'month', 'day', 'hour', 'day_of_week', 'week', 'quarter', 'from_iso8601_timestamp', 'at_timezone', 'cardinality',
    'contains', 'element_at', 'array_join', 'filter', 'transform', 'reduce', 'sequence', 'row_number', 'rank',
    'dense_rank', 'lag', 'lead', 'first_value', 'last_value', 'ntile', 'percent_rank', 'cume_dist',
];

const IDENT = /^[A-Za-z_][A-Za-z0-9_]*$/;
const TABLE_CONTEXT = new Set(['FROM', 'JOIN', 'INTO', 'UPDATE', 'TABLE', 'DESCRIBE']);
const RESERVED = new Set(KEYWORDS.flatMap((k) => k.split(' ')).concat(['LEFT', 'RIGHT', 'FULL', 'INNER', 'OUTER', 'CROSS',
    'NATURAL', 'LATERAL', 'BY', 'FOR', 'VERSION', 'TIMESTAMP', 'OF']));

/** An identifier as SQL needs it: plain when it can be, else double-quoted. */
export function quoteIdent(name: string): string {
    return IDENT.test(name) && !RESERVED.has(name.toUpperCase()) ? name : `"${name.replace(/"/g, '""')}"`;
}

function quoteName(dotted: string): string {
    return dotted.split('.').map(quoteIdent).join('.');
}

const unquote = (s: string) => (s.startsWith('"') && s.endsWith('"') ? s.slice(1, -1).replace(/""/g, '"') : s);

/** The text with strings and comments blanked (kept the same length, so positions still hold). */
export function blank(sql: string): { text: string; inString: (pos: number) => boolean } {
    const out = sql.split('');
    const hidden: [number, number][] = [];
    let i = 0;
    while (i < sql.length) {
        const c = sql[i];
        if (c === "'" || (c === '-' && sql[i + 1] === '-') || (c === '/' && sql[i + 1] === '*')) {
            const start = i;
            if (c === "'") {
                i++;
                while (i < sql.length && !(sql[i] === "'" && sql[i + 1] !== "'")) i += sql[i] === "'" ? 2 : 1;
                i = Math.min(i + 1, sql.length + 1);
            } else if (c === '-') {
                while (i < sql.length && sql[i] !== '\n') i++;
                if (i >= sql.length) i = sql.length + 1; // runs to the end: the end is inside it
            } else {
                const end = sql.indexOf('*/', i + 2);
                i = end < 0 ? sql.length + 1 : end + 2;
            }
            for (let k = start; k < Math.min(i, sql.length); k++) if (out[k] !== '\n') out[k] = ' ';
            hidden.push([start, i]);
            continue;
        }
        i++;
    }
    return { text: out.join(''), inString: (pos) => hidden.some(([a, b]) => pos > a && pos < b) };
}

interface Source {
    table: SchemaTable;
    alias?: string;
}

/** The tables the query reads (FROM and JOIN), with their aliases. */
export function sources(text: string, tables: SchemaTable[]): Source[] {
    const byName = new Map<string, SchemaTable>();
    for (const t of tables) {
        byName.set(t.name.toLowerCase(), t);
        const short = t.name.split('.').slice(-1)[0].toLowerCase();
        if (!byName.has(short)) byName.set(short, t);
        const two = t.name.split('.').slice(-2).join('.').toLowerCase();
        if (!byName.has(two)) byName.set(two, t);
    }
    const out: Source[] = [];
    const re = /\b(?:FROM|JOIN)\s+((?:"[^"]+"|[A-Za-z_][\w$]*)(?:\s*\.\s*(?:"[^"]+"|[A-Za-z_][\w$]*))*)(?:\s+(?:AS\s+)?("?[A-Za-z_][\w$]*"?))?/gi;
    let m: RegExpExecArray | null;
    while ((m = re.exec(text))) {
        const name = m[1].split('.').map((p) => unquote(p.trim())).join('.').toLowerCase();
        const t = byName.get(name);
        if (!t) continue;
        const alias = m[2] && !RESERVED.has(unquote(m[2]).toUpperCase()) ? unquote(m[2]) : undefined;
        out.push({ table: t, alias });
    }
    return out;
}

function rank(items: Suggestion[], prefix: string): Suggestion[] {
    const p = prefix.toLowerCase();
    const scored = items
        .map((s) => {
            const l = s.label.toLowerCase();
            const last = l.split('.').slice(-1)[0];
            const score = !p ? 1 : l.startsWith(p) || last.startsWith(p) ? 3 : l.includes(p) ? 2 : 0;
            return { s, score };
        })
        .filter((x) => x.score > 0);
    const order: Record<Kind, number> = { column: 0, table: 1, schema: 2, function: 3, keyword: 4 };
    scored.sort((a, b) => b.score - a.score || order[a.s.kind] - order[b.s.kind] || a.s.label.localeCompare(b.s.label));
    const seen = new Set<string>();
    return scored.map((x) => x.s).filter((s) => (seen.has(s.kind + s.label) ? false : (seen.add(s.kind + s.label), true)));
}

/** What may come at ``cursor`` in ``sql``, given the tables the person may query. */
export function complete(sql: string, cursor: number, tables: SchemaTable[], limit = 50): Completion {
    const { text, inString } = blank(sql);
    if (inString(cursor)) return { from: cursor, to: cursor, items: [] };
    const before = text.slice(0, cursor);
    const word = /("?[A-Za-z_][\w$]*"?|"[^"]*)?$/.exec(before)?.[0] ?? '';
    const from = cursor - word.length;
    const prefix = word.replace(/^"/, '').replace(/"$/, '');
    const head = before.slice(0, from);

    // name. → the schema's tables, or the table's or alias's columns
    const dotted = /((?:"[^"]+"|[A-Za-z_][\w$]*)(?:\.(?:"[^"]+"|[A-Za-z_][\w$]*))*)\.\s*$/.exec(head);
    const prev = /([A-Za-z_]+)\s+$/.exec(head)?.[1]?.toUpperCase();
    const clauses = head.match(/\b(SELECT|FROM|JOIN|WHERE|GROUP|ORDER|HAVING|ON|LIMIT|SET|INTO|UPDATE)\b/gi) ?? [];
    const lastClause = clauses.length ? clauses[clauses.length - 1].toUpperCase() : '';
    const inTableSpot = (!!prev && TABLE_CONTEXT.has(prev)) || (/,\s*$/.test(head) && lastClause === 'FROM');
    if (dotted) {
        const q = dotted[1].split('.').map(unquote).join('.').toLowerCase();
        const items: Suggestion[] = [];
        for (const t of tables) {
            const n = t.name.toLowerCase();
            if (n.startsWith(q + '.') && n.split('.').length === q.split('.').length + 1) {
                const last = t.name.split('.').slice(-1)[0];
                items.push({ label: last, insert: quoteIdent(last), kind: 'table', detail: t.name });
            } else if (n.startsWith(q + '.')) {
                const part = t.name.split('.')[q.split('.').length];
                items.push({ label: part, insert: quoteIdent(part), kind: 'schema' });
            }
        }
        if (!inTableSpot) {
            const srcs = sources(text, tables);
            const hit = srcs.find((s) => s.alias?.toLowerCase() === q)
                ?? srcs.find((s) => s.table.name.toLowerCase() === q || s.table.name.toLowerCase().endsWith('.' + q))
                ?? (tables.find((t) => t.name.toLowerCase() === q) ? { table: tables.find((t) => t.name.toLowerCase() === q)! } : undefined);
            if (hit) {
                for (const c of hit.table.columns) items.push({ label: c.name, insert: quoteIdent(c.name), kind: 'column', detail: c.type });
            }
        }
        return { from, to: cursor, items: rank(items, prefix).slice(0, limit) };
    }
    if (inTableSpot) {
        const items: Suggestion[] = [];
        const schemas = new Set<string>();
        for (const t of tables) {
            items.push({ label: t.name, insert: quoteName(t.name), kind: 'table', detail: `${t.columns.length} columns` });
            if (t.name.includes('.')) schemas.add(t.name.split('.').slice(0, -1).join('.'));
        }
        for (const s of schemas) items.push({ label: s, insert: quoteName(s), kind: 'schema' });
        return { from, to: cursor, items: rank(items, prefix).slice(0, limit) };
    }
    const srcs = sources(text, tables);
    const counts = new Map<string, number>();
    for (const s of srcs) for (const c of s.table.columns) counts.set(c.name, (counts.get(c.name) ?? 0) + 1);
    const items: Suggestion[] = [];
    for (const s of srcs) {
        const qual = s.alias ?? s.table.name.split('.').slice(-1)[0];
        for (const c of s.table.columns) {
            const ambiguous = (counts.get(c.name) ?? 0) > 1;
            items.push({
                label: ambiguous ? `${qual}.${c.name}` : c.name,
                insert: ambiguous ? `${quoteIdent(qual)}.${quoteIdent(c.name)}` : quoteIdent(c.name),
                kind: 'column',
                detail: `${c.type} · ${s.table.name}`,
            });
        }
    }
    if (prefix) {
        for (const f of FUNCTIONS) items.push({ label: f, insert: `${f}(`, kind: 'function' });
        for (const k of KEYWORDS) items.push({ label: k, insert: k, kind: 'keyword' });
        if (!srcs.length) {
            for (const t of tables) items.push({ label: t.name, insert: quoteName(t.name), kind: 'table' });
        }
    }
    return { from, to: cursor, items: rank(items, prefix).slice(0, limit) };
}

/** ``sql`` with the suggestion put in place of the word at the cursor, and where the cursor goes. */
export function accept(sql: string, c: Completion, s: Suggestion): { sql: string; cursor: number } {
    const out = sql.slice(0, c.from) + s.insert + sql.slice(c.to);
    return { sql: out, cursor: c.from + s.insert.length };
}
