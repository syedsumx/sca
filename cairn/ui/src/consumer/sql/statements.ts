/**
 * A SQL file as statements (L163): where each starts and ends, so the editor runs the one at the cursor, the
 * selection, or all of them one after another. A ``;`` ends a statement only outside strings ('…', with '' inside),
 * quoted names ("…"), backquoted names (`…`) and comments (-- … and /* … *\/). Empty statements (only blanks and
 * comments) are left out.
 */

export interface Statement {
    /** The statement's text, without its ``;`` and the blanks around it. */
    text: string;
    /** Where it starts and ends in the file (the text's own range: ``sql.slice(from, to) === text``). */
    from: number;
    to: number;
}

/** True when ``text`` holds something other than blanks and comments. */
function hasCode(text: string): boolean {
    return stripComments(text).trim().length > 0;
}

function stripComments(text: string): string {
    let out = '';
    let i = 0;
    while (i < text.length) {
        const c = text[i];
        const n = text[i + 1];
        if (c === '-' && n === '-') {
            const end = text.indexOf('\n', i);
            i = end < 0 ? text.length : end;
        } else if (c === '/' && n === '*') {
            const end = text.indexOf('*/', i + 2);
            i = end < 0 ? text.length : end + 2;
        } else if (c === "'" || c === '"' || c === '`') {
            const end = closing(text, i);
            out += text.slice(i, end);
            i = end;
        } else {
            out += c;
            i += 1;
        }
    }
    return out;
}

/** Where the string or quoted name opened at ``i`` ends (after its closing quote; the end of text when open). */
function closing(text: string, i: number): number {
    const q = text[i];
    let j = i + 1;
    while (j < text.length) {
        if (text[j] === q) {
            if (text[j + 1] === q) {          // '' or "" inside: an escaped quote
                j += 2;
                continue;
            }
            return j + 1;
        }
        j += 1;
    }
    return text.length;
}

/** The statements of ``sql``, in order. */
export function splitStatements(sql: string): Statement[] {
    const out: Statement[] = [];
    let start = 0;
    let i = 0;
    const push = (end: number) => {
        const raw = sql.slice(start, end);
        if (hasCode(raw)) {
            const lead = raw.length - raw.trimStart().length;
            const text = raw.trim();
            out.push({ text, from: start + lead, to: start + lead + text.length });
        }
    };
    while (i < sql.length) {
        const c = sql[i];
        const n = sql[i + 1];
        if (c === '-' && n === '-') {
            const end = sql.indexOf('\n', i);
            i = end < 0 ? sql.length : end;
        } else if (c === '/' && n === '*') {
            const end = sql.indexOf('*/', i + 2);
            i = end < 0 ? sql.length : end + 2;
        } else if (c === "'" || c === '"' || c === '`') {
            i = closing(sql, i);
        } else if (c === ';') {
            push(i);
            start = i + 1;
            i += 1;
        } else {
            i += 1;
        }
    }
    push(sql.length);
    return out;
}

/**
 * The statement the cursor is in or just after (a cursor after a statement's ``;`` on the same line, or in the blank
 * lines that follow it, still means that statement, as editors do); the first one when the cursor is before all.
 */
export function statementAt(statements: Statement[], cursor: number): Statement | null {
    if (!statements.length) return null;
    let best: Statement | null = null;
    for (const s of statements) {
        if (s.from <= cursor) best = s;
        else break;
    }
    return best ?? statements[0];
}

/**
 * What to run: ``all`` the file's statements; otherwise the selection's statements when something is selected, else the
 * statement at the cursor.
 */
export function toRun(sql: string, selection: { from: number; to: number }, mode: 'current' | 'all'): Statement[] {
    if (mode === 'all') return splitStatements(sql);
    if (selection.to > selection.from) {
        return splitStatements(sql.slice(selection.from, selection.to))
            .map((s) => ({ ...s, from: s.from + selection.from, to: s.to + selection.from }));
    }
    const one = statementAt(splitStatements(sql), selection.from);
    return one ? [one] : [];
}

/** A short title for a statement's result tab: its first words, on one line. */
export function statementTitle(text: string, max = 40): string {
    const one = stripComments(text).replace(/\s+/g, ' ').trim();
    return one.length > max ? `${one.slice(0, max - 1)}…` : one;
}
