// Builds the in-product documentation (docs/site/*.md) into static pages served at /docs/.
//
//   node scripts/build-docs.mjs [srcDir] [outDir]     (defaults: ../docs/site → dist/docs)
//
// Fails when a page links to another page or anchor that does not exist, or names a vendor the
// product must not mention.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Marked } from 'marked';

const here = path.dirname(fileURLToPath(import.meta.url));
const src = path.resolve(process.argv[2] || path.join(here, '../../docs/site'));
const out = path.resolve(process.argv[3] || path.join(here, '../dist/docs'));
const brand = path.join(here, '../brand/cairn-logo.svg');
const LEAK = /data\s?hub|acryl|atlan(?![a-z])/i;
// Absolute paths outside /docs/ that the docs may link to (served by other Cairn components).
const EXTERNAL_OK = [/^\/cairn\/api\/docs$/];

const slug = (s) => s.toLowerCase().replace(/<[^>]+>/g, '').replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
const esc = (s) => s.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);

function parse(file) {
    const raw = fs.readFileSync(path.join(src, file), 'utf8');
    const m = /^---\n([\s\S]*?)\n---\n/.exec(raw);
    const meta = Object.fromEntries((m ? m[1] : '').split('\n').filter(Boolean)
        .map((l) => [l.slice(0, l.indexOf(':')).trim(), l.slice(l.indexOf(':') + 1).trim()]));
    return { file, name: file.replace(/\.md$/, '.html'), title: meta.title || file, order: Number(meta.order ?? 99),
        body: m ? raw.slice(m[0].length) : raw };
}

const pages = fs.readdirSync(src).filter((f) => f.endsWith('.md')).map(parse).sort((a, b) => a.order - b.order);
const logo = fs.readFileSync(brand, 'utf8');

const CSS = `
:root{--bg:#fff;--fg:#1f1d2b;--muted:#5d5a6e;--line:#e7e5f0;--accent:#533fd1;--code:#f5f4fa;--side:#f8f7fc}
@media (prefers-color-scheme:dark){:root{--bg:#15141c;--fg:#ecebf3;--muted:#a9a6ba;--line:#2c2a38;--accent:#9b8cf0;--code:#1e1c27;--side:#1a1922}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:var(--accent)}header{display:flex;align-items:center;gap:10px;padding:12px 20px;border-bottom:1px solid var(--line)}
header svg{width:22px;height:22px}header a{color:var(--fg);text-decoration:none;font-weight:600}
.wrap{display:grid;grid-template-columns:240px minmax(0,1fr);max-width:1180px;margin:0 auto}
nav{background:var(--side);border-right:1px solid var(--line);padding:20px 12px;min-height:calc(100vh - 50px)}
nav a{display:block;padding:6px 10px;border-radius:6px;color:var(--fg);text-decoration:none;font-size:15px}
nav a.on{background:color-mix(in srgb,var(--accent) 14%,transparent);color:var(--accent);font-weight:600}
main{padding:28px 40px 60px;min-width:0}h1{font-size:30px;margin-top:0}h2{margin-top:2em;padding-bottom:4px;border-bottom:1px solid var(--line)}
table{border-collapse:collapse;width:100%;margin:1em 0;display:block;overflow-x:auto}th,td{border:1px solid var(--line);padding:6px 10px;text-align:left;vertical-align:top}
code{background:var(--code);padding:1px 5px;border-radius:4px;font-size:.9em}pre{background:var(--code);padding:12px 14px;border-radius:8px;overflow-x:auto}pre code{padding:0}
@media (max-width:760px){.wrap{grid-template-columns:1fr}nav{min-height:0;border-right:0;border-bottom:1px solid var(--line);display:flex;flex-wrap:wrap}main{padding:20px 16px}}`;

const problems = [];
const anchors = new Map();
const rendered = pages.map((p) => {
    const ids = new Set();
    const md = new Marked();
    md.use({ renderer: {
        heading({ tokens, depth }) {
            const text = this.parser.parseInline(tokens);
            const id = slug(text);
            ids.add(id);
            return `<h${depth} id="${id}">${text}</h${depth}>\n`;
        },
    } });
    const html = md.parse(p.body);
    anchors.set(p.name, ids);
    return { ...p, html };
});

for (const p of rendered) {
    for (const [, href] of p.html.matchAll(/href="([^"]+)"/g)) {
        if (/^(https?:|mailto:)/.test(href)) continue;
        if (href.startsWith('/')) {
            if (!EXTERNAL_OK.some((rx) => rx.test(href))) problems.push(`${p.file}: absolute link ${href}`);
            continue;
        }
        const [target, anchor] = href.split('#');
        const page = target || p.name;
        if (!anchors.has(page)) problems.push(`${p.file}: broken link ${href}`);
        else if (anchor && !anchors.get(page).has(anchor)) problems.push(`${p.file}: missing anchor ${href}`);
    }
    if (LEAK.test(p.html) || LEAK.test(p.title)) problems.push(`${p.file}: names another vendor (${LEAK.exec(p.html + p.title)[0]})`);
}
if (problems.length) {
    console.error(problems.join('\n'));
    process.exit(1);
}

fs.rmSync(out, { recursive: true, force: true });
fs.mkdirSync(out, { recursive: true });
for (const p of rendered) {
    const nav = rendered.map((q) => `<a href="${q.name}"${q === p ? ' class="on" aria-current="page"' : ''}>${esc(q.title)}</a>`).join('');
    const title = p.order === 0 ? 'Cairn documentation' : `${p.title} · Cairn documentation`;
    fs.writeFileSync(path.join(out, p.name), `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${esc(title)}</title><link rel="icon" href="/assets/icons/favicon.ico"><style>${CSS}</style></head>
<body><header>${logo}<a href="index.html">Cairn documentation</a><span style="flex:1"></span><a href="/">Open Cairn →</a></header>
<div class="wrap"><nav aria-label="Documentation">${nav}</nav><main>${p.html}</main></div></body></html>
`);
}
console.log(`docs: ${rendered.length} pages → ${out}`);
