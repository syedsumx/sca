// The Capabilities page's data, read from ROADMAP.md: the platform's layers ("The platform today") and every
// numbered item ("Next"), with its status. `node scripts/capabilities.mjs` rewrites
// src/consumer/resources/capabilities.json; a test fails when that file and ROADMAP.md disagree.
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const STATUS = { '✅': 'shipped', '🟡': 'partial', '⬜': 'planned' };

/** Plain text from a markdown cell: no bold, code ticks or links. */
function plain(s) {
    return s.replace(/\*\*(.+?)\*\*/g, '$1').replace(/`([^`]+)`/g, '$1').replace(/\[([^\]]+)\]\([^)]+\)/g, '$1').trim();
}

function cells(line) {
    return line.replace(/^\|/, '').replace(/\|\s*$/, '').split(/(?<!\\)\|/).map((c) => c.trim());
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
    for (const line of section(md, 'Next').split('\n')) {
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
