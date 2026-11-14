/**
 * YAML for flow definitions, with no dependency: enough of YAML for what people write by hand in a flow (block
 * mappings and lists, `[a, b]` and `{x: 1}` on a line, quoted and plain scalars, `|` and `>` blocks, comments) and
 * the same layout Cairn writes. What it does not read (anchors, tags, several documents) is an error with its line,
 * never a guess.
 */

export class YamlError extends Error {
    readonly line: number;

    constructor(message: string, line: number) {
        super(`line ${line}: ${message}`);
        this.name = 'YamlError';
        this.line = line;
    }
}

interface Line {
    no: number;
    indent: number;
    text: string;
}

type Value = unknown;

const INT = /^[-+]?(0|[1-9][0-9]*)$/;
const FLOAT = /^[-+]?(\d+\.\d*|\.\d+|\d+(\.\d*)?[eE][-+]?\d+)$/;

function stripComment(s: string): string {
    let q: string | null = null;
    for (let i = 0; i < s.length; i++) {
        const c = s[i];
        if (q) {
            if (c === q) {
                if (q === "'" && s[i + 1] === "'") i++;
                else q = null;
            } else if (q === '"' && c === '\\') i++;
        } else if (c === '"' || c === "'") {
            if (i === 0 || /[\s[{,:]/.test(s[i - 1])) q = c;
        } else if (c === '#' && (i === 0 || /\s/.test(s[i - 1]))) {
            return s.slice(0, i).replace(/\s+$/, '');
        }
    }
    return s.replace(/\s+$/, '');
}

function scalar(raw: string, no: number): Value {
    const s = raw.trim();
    if (s === '' || s === '~' || s === 'null' || s === 'Null' || s === 'NULL') return null;
    if (s === 'true' || s === 'True' || s === 'TRUE') return true;
    if (s === 'false' || s === 'False' || s === 'FALSE') return false;
    if (s.startsWith("'")) {
        if (!s.endsWith("'") || s.length < 2) throw new YamlError('a quoted text has no closing quote', no);
        return s.slice(1, -1).replace(/''/g, "'");
    }
    if (s.startsWith('"')) {
        if (!s.endsWith('"') || s.length < 2) throw new YamlError('a quoted text has no closing quote', no);
        try {
            return JSON.parse(s);
        } catch {
            throw new YamlError(`cannot read ${s}`, no);
        }
    }
    if (/^[&*!%@`]/.test(s)) throw new YamlError(`${s[0]} (anchors, aliases, tags) is not supported here`, no);
    if (INT.test(s)) return parseInt(s, 10);
    if (FLOAT.test(s)) return parseFloat(s);
    return s;
}

/** A flow collection ([..] or {..}) starting at `text[i]`: [value, next index]. */
function flow(text: string, i: number, no: number): [Value, number] {
    const skip = () => {
        while (i < text.length && /\s/.test(text[i])) i++;
    };
    const item = (stops: string): Value => {
        skip();
        if (text[i] === '[' || text[i] === '{') {
            const [v, j] = flow(text, i, no);
            i = j;
            return v;
        }
        if (text[i] === '"' || text[i] === "'") {
            const q = text[i];
            let j = i + 1;
            for (; j < text.length; j++) {
                if (text[j] === '\\' && q === '"') j++;
                else if (text[j] === q) {
                    if (q === "'" && text[j + 1] === "'") j++;
                    else break;
                }
            }
            if (j >= text.length) throw new YamlError('a quoted text has no closing quote', no);
            const v = scalar(text.slice(i, j + 1), no);
            i = j + 1;
            return v;
        }
        let j = i;
        while (j < text.length && !stops.includes(text[j]) && !(text[j] === ':' && /[\s,\]}]/.test(text[j + 1] ?? ' ') && stops.includes(':')))
            j++;
        const v = scalar(text.slice(i, j), no);
        i = j;
        return v;
    };
    const open = text[i];
    i++;
    if (open === '[') {
        const out: Value[] = [];
        skip();
        if (text[i] === ']') return [out, i + 1];
        for (;;) {
            out.push(item(',]'));
            skip();
            if (text[i] === ',') {
                i++;
                skip();
                if (text[i] === ']') return [out, i + 1];
                continue;
            }
            if (text[i] === ']') return [out, i + 1];
            throw new YamlError("a list in [ ] is missing a ',' or its ']'", no);
        }
    }
    const out: Record<string, Value> = {};
    skip();
    if (text[i] === '}') return [out, i + 1];
    for (;;) {
        const k = item(':,}');
        skip();
        if (text[i] !== ':') throw new YamlError("a mapping in { } needs 'key: value'", no);
        i++;
        const v = item(',}');
        out[String(k)] = v;
        skip();
        if (text[i] === ',') {
            i++;
            skip();
            if (text[i] === '}') return [out, i + 1];
            continue;
        }
        if (text[i] === '}') return [out, i + 1];
        throw new YamlError("a mapping in { } is missing a ',' or its '}'", no);
    }
}

function balanced(s: string): boolean {
    let depth = 0;
    let q: string | null = null;
    for (let i = 0; i < s.length; i++) {
        const c = s[i];
        if (q) {
            if (c === '\\' && q === '"') i++;
            else if (c === q) q = null;
        } else if (c === '"' || c === "'") q = c;
        else if (c === '[' || c === '{') depth++;
        else if (c === ']' || c === '}') depth--;
    }
    return depth <= 0;
}

/** Where a 'key: value' line splits (-1 when it is not one). */
function keySplit(s: string): number {
    if (s.startsWith('"') || s.startsWith("'")) {
        const q = s[0];
        let j = 1;
        for (; j < s.length; j++) {
            if (s[j] === '\\' && q === '"') j++;
            else if (s[j] === q) break;
        }
        const rest = s.slice(j + 1);
        const m = /^\s*:(\s|$)/.exec(rest);
        return m ? j + 1 + rest.indexOf(':') : -1;
    }
    if (s.startsWith('[') || s.startsWith('{')) return -1;
    for (let i = 0; i < s.length; i++) {
        if (s[i] === ':' && (i + 1 === s.length || s[i + 1] === ' ')) return i;
    }
    return -1;
}

export function parse(src: string): Value {
    const raw = src.replace(/\r\n?/g, '\n').split('\n');
    if (raw.some((l) => /^---\s*$/.test(l)) && raw.filter((l) => /^---\s*$/.test(l)).length > 1)
        throw new YamlError('one flow per document', raw.findIndex((l, i) => i > 0 && /^---\s*$/.test(l)) + 1);
    let pos = 0;
    const lines: (Line | null)[] = raw.map((l, i) => {
        if (/\t/.test(l.replace(/^( *)/, '').slice(0, 0)) || /^\t/.test(l)) throw new YamlError('indent with spaces, not tabs', i + 1);
        const indent = l.length - l.replace(/^ +/, '').length;
        const text = l.slice(indent);
        if (text === '' || text.startsWith('#') || /^---\s*$/.test(text) || /^\.\.\.\s*$/.test(text)) return null;
        return { no: i + 1, indent, text };
    });
    const peek = (): Line | null => {
        while (pos < lines.length && lines[pos] === null) pos++;
        return pos < lines.length ? lines[pos] : null;
    };

    const blockScalar = (head: string, parentIndent: number, no: number): string => {
        const folded = head.startsWith('>');
        const keep = head.includes('+');
        const strip = head.includes('-');
        const body: string[] = [];
        let indent = -1;
        pos++;
        while (pos < raw.length) {
            const l = raw[pos];
            const ind = l.length - l.replace(/^ +/, '').length;
            if (l.trim() === '') {
                body.push('');
                pos++;
                continue;
            }
            if (indent < 0) indent = ind;
            if (ind < indent || ind <= parentIndent) break;
            body.push(l.slice(indent));
            pos++;
        }
        if (indent < 0 && body.length === 0) return '';
        while (!keep && body.length && body[body.length - 1] === '') body.pop();
        let text = folded ? body.reduce((acc, l, i) => (i === 0 ? l : acc + (l === '' ? '\n' : (acc.endsWith('\n') ? '' : ' ') + l)), '') : body.join('\n');
        if (!strip) text += '\n';
        void no;
        return text;
    };

    const inline = (text: string, l: Line, indent: number): Value => {
        const t = text.trim();
        if (t.startsWith('|') || t.startsWith('>')) {
            if (!/^[|>][+-]?$/.test(t)) throw new YamlError(`cannot read the block marker ${t}`, l.no);
            const v = blockScalar(t, indent, l.no);
            return v;
        }
        if (t.startsWith('[') || t.startsWith('{')) {
            let joined = t;
            const start = pos;
            while (!balanced(joined) && pos + 1 < raw.length) {
                pos++;
                joined += ' ' + stripComment(raw[pos].trim());
            }
            const [v, end] = flow(joined, 0, l.no);
            if (joined.slice(end).trim()) throw new YamlError(`unexpected ${joined.slice(end).trim()} after a ${t[0]} ${t[0] === '[' ? ']' : '}'}`, l.no);
            pos = pos === start ? pos + 1 : pos + 1;
            return v;
        }
        pos++;
        return scalar(stripComment(t), l.no);
    };

    const block = (indent: number): Value => {
        const l = peek();
        if (!l || l.indent < indent) return null;
        if (l.text === '-' || l.text.startsWith('- ')) return sequence(l.indent);
        return mapping(l.indent);
    };

    const sequence = (indent: number): Value[] => {
        const out: Value[] = [];
        for (;;) {
            const l = peek();
            if (!l || l.indent < indent) return out;
            if (l.indent > indent) throw new YamlError('this line is indented more than the list it is in', l.no);
            if (!(l.text === '-' || l.text.startsWith('- '))) return out;
            const content = l.text === '-' ? '' : l.text.slice(2).replace(/^ +/, '');
            const at = l.indent + (l.text.length - content.length);
            if (stripComment(content) === '') {
                pos++;
                const next = peek();
                out.push(next && next.indent > indent ? block(next.indent) : null);
            } else if (keySplit(stripComment(content)) > 0 && !content.startsWith('[') && !content.startsWith('{')) {
                lines[pos] = { no: l.no, indent: at, text: content };   // the item is a mapping that starts on this line
                out.push(mapping(at));
            } else {
                out.push(inline(content, l, indent));
            }
        }
    };

    const mapping = (indent: number): Record<string, Value> => {
        const out: Record<string, Value> = {};
        for (;;) {
            const l = peek();
            if (!l || l.indent < indent) return out;
            if (l.indent > indent) throw new YamlError('this line is indented more than the mapping it is in', l.no);
            if (l.text.startsWith('- ') || l.text === '-') {
                throw new YamlError("a list item where a 'key: value' was expected", l.no);
            }
            const text = stripComment(l.text);
            const k = keySplit(text);
            if (k <= 0) throw new YamlError(`expected 'key: value', got ${text.slice(0, 40)}`, l.no);
            const keyRaw = text.slice(0, k).trim();
            const key = keyRaw.startsWith('"') || keyRaw.startsWith("'") ? String(scalar(keyRaw, l.no)) : keyRaw;
            if (Object.prototype.hasOwnProperty.call(out, key)) throw new YamlError(`${key} is given twice`, l.no);
            const rest = l.text.slice(l.text.indexOf(':', keyRaw.length) + 1);
            if (stripComment(rest).trim() === '') {
                pos++;
                const next = peek();
                if (next && next.indent > indent) out[key] = block(next.indent);
                else if (next && next.indent === indent && (next.text.startsWith('- ') || next.text === '-')) out[key] = sequence(indent);
                else out[key] = null;
            } else {
                out[key] = inline(rest, l, indent);
            }
        }
    };

    const first = peek();
    if (!first) return null;
    const value = block(first.indent);
    const extra = peek();
    if (extra) throw new YamlError('this line does not belong to anything above it (check its indent)', extra.no);
    return value;
}

// ---- writing ---------------------------------------------------------------------------------------------
const PLAIN_SAFE = /^[A-Za-z0-9_./($+=<-][^:#]*$/;

function quoteIfNeeded(s: string): string {
    if (s === '') return "''";
    const looksTyped = INT.test(s) || FLOAT.test(s) || /^(true|false|null|~|yes|no|on|off|y|n)$/i.test(s);
    const risky = !PLAIN_SAFE.test(s) || /:\s|\s#|:$/.test(s) || /^\s|\s$/.test(s) || /^- /.test(s) || /^[-?](\s|$)/.test(s);
    if (looksTyped || risky || s.includes('{{') || /[[\]{},]/.test(s[0])) return `'${s.replace(/'/g, "''")}'`;
    return s;
}

function flowItem(v: Value): string {
    if (typeof v === 'string') {
        const q = quoteIfNeeded(v);
        return /[,[\]{}]/.test(v) && !q.startsWith("'") ? `'${v.replace(/'/g, "''")}'` : q;
    }
    if (v === null || v === undefined) return 'null';
    return String(v);
}

const isScalar = (v: Value) => v === null || ['string', 'number', 'boolean'].includes(typeof v);

function scalarText(v: Value): string {
    if (v === null || v === undefined) return 'null';
    if (typeof v === 'string') return quoteIfNeeded(v);
    if (typeof v === 'number') return Number.isFinite(v) ? String(v) : 'null';
    return String(v);
}

function emit(v: Value, indent: number, out: string[], key: string | null, inList: boolean): void {
    const pad = ' '.repeat(indent);
    const lead = key !== null ? `${pad}${key}:` : `${pad}-`;
    if (typeof v === 'string' && v.includes('\n')) {
        const body = v.endsWith('\n') ? v.slice(0, -1) : v;
        out.push(`${lead} |${v.endsWith('\n') ? '' : '-'}`);
        for (const l of body.split('\n')) out.push(l === '' ? '' : `${pad}  ${l}`);
        return;
    }
    if (isScalar(v)) {
        out.push(`${lead} ${scalarText(v)}`);
        return;
    }
    if (Array.isArray(v)) {
        if (v.length === 0) {
            out.push(`${lead} []`);
            return;
        }
        if (v.length <= 12 && v.every((x) => isScalar(x) && String(x).length <= 60 && !String(x).includes('\n'))) {
            out.push(`${lead} [${v.map(flowItem).join(', ')}]`);
            return;
        }
        if (key !== null) out.push(lead);
        else if (inList) out.push(lead);
        for (const item of v) emit(item, key !== null ? indent : indent + 2, out, null, true);
        return;
    }
    const obj = v as Record<string, Value>;
    const keys = Object.keys(obj).filter((k) => obj[k] !== undefined);
    if (keys.length === 0) {
        out.push(`${lead} {}`);
        return;
    }
    if (keys.length <= 3 && keys.every((k) => k === 'x' || k === 'y')) {
        out.push(`${lead} {${keys.map((k) => `${k}: ${scalarText(obj[k])}`).join(', ')}}`);
        return;
    }
    if (key !== null) {
        out.push(lead);
        for (const k of keys) emit(obj[k], indent + 2, out, quoteKey(k), false);
        return;
    }
    // A mapping as a list item: its first key on the dash's line.
    const tmp: string[] = [];
    for (const k of keys) emit(obj[k], indent + 2, tmp, quoteKey(k), false);
    tmp[0] = `${pad}- ${tmp[0].slice(indent + 2)}`;
    out.push(...tmp);
}

function quoteKey(k: string): string {
    return /^[A-Za-z_][A-Za-z0-9_-]*$/.test(k) ? k : `'${k.replace(/'/g, "''")}'`;
}

export function stringify(v: Value): string {
    const out: string[] = [];
    if (v && typeof v === 'object' && !Array.isArray(v)) {
        for (const [k, x] of Object.entries(v as Record<string, Value>)) if (x !== undefined) emit(x, 0, out, quoteKey(k), false);
    } else if (Array.isArray(v)) {
        for (const x of v) emit(x, 0, out, null, true);
    } else {
        out.push(scalarText(v));
    }
    return out.join('\n') + '\n';
}
