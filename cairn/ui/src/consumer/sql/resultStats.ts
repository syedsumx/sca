/**
 * A query result in the editor (L163): which rows match the person's column filters, what each column holds (its
 * counts, range and most common values), and the visible part as text to paste into a spreadsheet. Pure functions
 * over the rows the editor already has: nothing is sent anywhere.
 */

export type Cell = unknown;

/** Filters by column: a row matches when each filtered column's text contains the filter (case ignored). */
export type ColumnFilters = Record<string, string>;

export function cellText(v: Cell): string {
    if (v === null || v === undefined) return '';
    if (typeof v === 'object') return JSON.stringify(v);
    return String(v);
}

/** The indexes of the rows that match every filter. */
export function matching(columns: string[], rows: Cell[][], filters: ColumnFilters): number[] {
    const active = Object.entries(filters)
        .map(([c, f]) => [columns.indexOf(c), f.trim().toLowerCase()] as const)
        .filter(([i, f]) => i >= 0 && f);
    const out: number[] = [];
    rows.forEach((r, n) => {
        if (active.every(([i, f]) => (f === 'null' ? r[i] === null || r[i] === undefined : cellText(r[i]).toLowerCase().includes(f)))) {
            out.push(n);
        }
    });
    return out;
}

/** The columns whose name contains ``search`` (all when it is empty). */
export function shownColumns(columns: string[], search: string): string[] {
    const s = search.trim().toLowerCase();
    return s ? columns.filter((c) => c.toLowerCase().includes(s)) : columns;
}

export interface ColumnStats {
    column: string;
    kind: 'number' | 'text' | 'boolean' | 'date' | 'empty' | 'mixed';
    count: number;
    nulls: number;
    distinct: number;
    min?: string | number;
    max?: string | number;
    mean?: number;
    /** The most common values (at most 5), most common first. */
    top: { value: string; count: number }[];
}

const DATE = /^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$/;

function kindOf(v: Cell): ColumnStats['kind'] {
    if (typeof v === 'number' || typeof v === 'bigint') return 'number';
    if (typeof v === 'boolean') return 'boolean';
    if (typeof v === 'string' && DATE.test(v)) return 'date';
    return 'text';
}

/** What a column holds, over ``rows`` (the matching ones, so the stats follow the filters). */
export function columnStats(column: string, index: number, rows: Cell[][]): ColumnStats {
    let nulls = 0;
    const counts = new Map<string, number>();
    const kinds = new Set<ColumnStats['kind']>();
    let min: number | string | undefined;
    let max: number | string | undefined;
    let sum = 0;
    let numbers = 0;
    for (const r of rows) {
        const v = r[index];
        if (v === null || v === undefined) {
            nulls += 1;
            continue;
        }
        const k = kindOf(v);
        kinds.add(k);
        const text = cellText(v);
        counts.set(text, (counts.get(text) ?? 0) + 1);
        const key: number | string = k === 'number' ? Number(v) : text;
        if (min === undefined || key < min) min = key;
        if (max === undefined || key > max) max = key;
        if (k === 'number') {
            sum += Number(v);
            numbers += 1;
        }
    }
    const kind: ColumnStats['kind'] = kinds.size === 0 ? 'empty' : kinds.size === 1 ? [...kinds][0] : 'mixed';
    const top = [...counts.entries()].sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1)).slice(0, 5)
        .map(([value, count]) => ({ value, count }));
    return {
        column, kind, count: rows.length, nulls, distinct: counts.size,
        ...(kind === 'mixed' || kind === 'empty' ? {} : { min, max }),
        ...(kind === 'number' && numbers ? { mean: sum / numbers } : {}),
        top,
    };
}

/** Tab-separated text (a header line, then the rows): pastes into a spreadsheet as cells. Tabs and line breaks
 * inside a value become spaces, so each value stays in its cell. */
export function toTsv(columns: string[], rows: Cell[][]): string {
    const clean = (s: string) => s.replace(/[\t\r\n]+/g, ' ');
    return [columns.map(clean).join('\t'), ...rows.map((r) => r.map((v) => clean(cellText(v))).join('\t'))].join('\n');
}

/** ``bytes`` in a short human form (1.2 MB). */
export function bytes(n: number | undefined | null): string {
    if (n == null || !Number.isFinite(n)) return '';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let v = n;
    let u = 0;
    while (v >= 1000 && u < units.length - 1) {
        v /= 1000;
        u += 1;
    }
    return `${u ? v.toFixed(1) : v} ${units[u]}`;
}
