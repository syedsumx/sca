// The platform map's data, read from ROADMAP.md: the platform's layers ("The platform today") and every
// numbered item ("Next"), with its status, and `new` on the rows the "In this release" line names. `node scripts/capabilities.mjs` rewrites
// src/consumer/resources/capabilities.json; a test fails when that file and ROADMAP.md disagree.
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const STATUS = { '✅': 'shipped', '🟡': 'partial', '⬜': 'planned' };

/** Plain text from a markdown cell: no bold, code ticks or links. */
function plain(s) {
    return s.replace(/\*\*(.+?)\*\*/g, '$1').replace(/`([^`]+)`/g, '$1').replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
        .replace(/\\\|/g, '|').trim();
}

/** A table row's cells: split on `|` outside code spans (`a | b` is one cell's text), and not on an escaped `\|`. */
export function cells(line) {
    const body = line.replace(/^\|/, '').replace(/\|\s*$/, '');
    const out = [];
    let cell = '';
    let code = false;
    for (let i = 0; i < body.length; i += 1) {
        const ch = body[i];
        if (ch === '\\' && body[i + 1] === '|') { cell += '\\|'; i += 1; continue; }
        if (ch === '`') code = !code;
        if (ch === '|' && !code) { out.push(cell.trim()); cell = ''; continue; }
        cell += ch;
    }
    out.push(cell.trim());
    return out;
}

function section(md, heading) {
    const start = md.indexOf(`\n## ${heading}`);
    if (start < 0) return '';
    const end = md.indexOf('\n## ', start + 4);
    return md.slice(start, end < 0 ? undefined : end);
}

function status(cell) {
    const mark = Object.keys(STATUS).find((m) => cell.startsWith(m));
    return { status: mark ? STATUS[mark] : 'planned', note: plain(mark ? cell.slice(mark.length) : cell) };
}

export function parseRoadmap(md) {
    const layers = [];
    for (const line of section(md, 'The platform today').split('\n')) {
        if (!line.startsWith('| ') || line.startsWith('| Layer') || line.startsWith('|---')) continue;
        const [layer, compare, cairn, st] = cells(line);
        layers.push({ layer: plain(layer), compare: plain(compare), cairn: plain(cairn), ...status(st || '') });
    }
    const items = [];
    const next = section(md, 'Next');
    // "**In this release:** L139 to L146." marks the newest rows; the platform map flags them as new.
    const release = /\*\*In this release:\*\*\s*L(\d+)\s*(?:to|–|-)\s*L(\d+)/.exec(next);
    const isNew = (id) => {
        const n = Number(id.slice(1));
        return !!release && n >= Number(release[1]) && n <= Number(release[2]);
    };
    for (const line of next.split('\n')) {
        const m = /^\| (L\d+) \|/.exec(line);
        if (!m) continue;
        const [id, item, done, st] = cells(line);
        const title = /\*\*(.+?)\*\*/.exec(item);
        items.push({
            id,
            title: plain(title ? title[1] : item).replace(/\.$/, ''),
            summary: plain(title ? item.replace(title[0], '') : ''),
            done: plain(done || ''),
            ...status(st || ''),
            ...(isNew(id) ? { new: true } : {}),
        });
    }
    return { layers, items };
}

const here = dirname(fileURLToPath(import.meta.url));
if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
    const md = readFileSync(join(here, '../../ROADMAP.md'), 'utf8');
    const out = join(here, '../src/consumer/resources/capabilities.json');
    const data = parseRoadmap(md);
    writeFileSync(out, JSON.stringify(data, null, 1) + '\n');
    console.log(`capabilities: ${data.layers.length} layers, ${data.items.length} items → ${out}`);
}
