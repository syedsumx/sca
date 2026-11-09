// Builds the contextual help's doc snippets (src/consumer/guide/help.generated.json) from the in-product docs
// (docs/site/*.md), so the help panel works with no server (the Vercel demo, offline) and opens at once.
//
//   node scripts/build-help.mjs            write the file
//   node scripts/build-help.mjs --check    fail when it is out of date (CI, and `npm test` checks it too)
//
// The sections wanted are the help topics of purpose-built pages (src/consumer/guide/help-topics.json) and the
// `docs` anchor of every platform area (src/consumer/resources/catalog.ts). Fails when one names a page or a heading
// the docs do not have.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const MAX_CHARS = 1600;

/** The heading's anchor, as scripts/build-docs.mjs makes it from the rendered heading. */
export const slug = (s) =>
    s.toLowerCase().replace(/<[^>]+>/g, '').replace(/[^\p{L}\p{N}]+/gu, '-').replace(/^-|-$/g, '');

/** Inline Markdown to the text a heading renders as (code, emphasis and links reduced to their words). */
const plain = (s) => s.replace(/`([^`]*)`/g, '$1').replace(/\*\*([^*]+)\*\*/g, '$1').replace(/\*([^*]+)\*/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1').trim();

/** Every section of one Markdown page: anchor → { title, level, body }. */
export function sections(markdown) {
    const lines = markdown.replace(/^---\n[\s\S]*?\n---\n/, '').split('\n');
    const heads = [];
    let fenced = false;
    lines.forEach((line, i) => {
        if (/^\s*```/.test(line)) fenced = !fenced;
        const m = !fenced && /^(#{1,6})\s+(.+?)\s*#*\s*$/.exec(line);
        if (m) heads.push({ i, level: m[1].length, title: plain(m[2]) });
    });
    const out = {};
    heads.forEach((h, n) => {
        const end = heads.slice(n + 1).find((x) => x.level <= h.level);
        const body = lines.slice(h.i + 1, end ? end.i : lines.length).join('\n').trim();
        const id = slug(h.title);
        if (!(id in out)) out[id] = { title: h.title, level: h.level, body };
    });
    return out;
}

/** The first paragraphs of a section, up to `max` characters, cut at a paragraph (or a list item). */
export function excerpt(body, max = MAX_CHARS) {
    const text = body.replace(/<!--[\s\S]*?-->/g, '').trim();
    if (text.length <= max) return { text, more: false };
    const blocks = text.split(/\n(?=\n|\* |- |\d+\. |#)/);
    let out = '';
    for (const b of blocks) {
        if ((out + b).length > max && out) break;
        out += b + '\n';
    }
    if (out.length > max * 1.5) out = out.slice(0, max).replace(/\s+\S*$/, '') + ' …';
    return { text: out.trim(), more: true };
}

/** Relative links between doc pages become links to the served docs (`/docs/<page>`). */
const linkDocs = (md, page) =>
    md.replace(/\]\((?!https?:|mailto:|\/)([^)#\s]*)(#[^)\s]*)?\)/g, (_, file, hash) => `](/docs/${file || page}${hash ?? ''})`);

export function wantedAnchors(root) {
    const topics = JSON.parse(fs.readFileSync(path.join(root, 'src/consumer/guide/help-topics.json'), 'utf8'));
    const catalog = fs.readFileSync(path.join(root, 'src/consumer/resources/catalog.ts'), 'utf8');
    const fromCatalog = [...catalog.matchAll(/"docs": "([^"]+)"/g)].map((m) => m[1]);
    return [...new Set([...Object.values(topics), ...fromCatalog])].sort();
}

export function buildHelp(docsDir, anchors) {
    const pages = {};
    const problems = [];
    const out = {};
    for (const anchor of anchors) {
        const [page, id] = anchor.split('#');
        const file = path.join(docsDir, page.replace(/\.html$/, '.md'));
        if (!pages[page]) {
            if (!fs.existsSync(file)) {
                problems.push(`${anchor}: no page ${page}`);
                continue;
            }
            pages[page] = sections(fs.readFileSync(file, 'utf8'));
        }
        const s = id ? pages[page][id] : undefined;
        if (!s) {
            problems.push(`${anchor}: no such heading in ${page}`);
            continue;
        }
        const { text, more } = excerpt(s.body);
        out[anchor] = { title: s.title, body: linkDocs(text, page), more };
    }
    return { snippets: out, problems };
}

const isMain = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
    const root = path.join(here, '..');
    const target = path.join(root, 'src/consumer/guide/help.generated.json');
    const { snippets, problems } = buildHelp(path.join(root, '../docs/site'), wantedAnchors(root));
    if (problems.length) {
        console.error(problems.join('\n'));
        process.exit(1);
    }
    const json = JSON.stringify(snippets, null, 1) + '\n';
    if (process.argv.includes('--check')) {
        const cur = fs.existsSync(target) ? fs.readFileSync(target, 'utf8') : '';
        if (cur !== json) {
            console.error('src/consumer/guide/help.generated.json is out of date: run node scripts/build-help.mjs');
            process.exit(1);
        }
        console.log('help snippets are up to date');
    } else {
        fs.writeFileSync(target, json);
        console.log(`${Object.keys(snippets).length} help snippets, ${(json.length / 1024).toFixed(0)} kB`);
    }
}
