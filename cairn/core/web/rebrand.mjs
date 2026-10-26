// Cairn white-label patch for the core web UI source (pinned upstream release).
//
//   node core/web/rebrand.mjs <web-ui-source-dir> <brand-dir>
//
// Only user-visible text changes: string literals, template text and JSX text are rewritten with
// the TypeScript parser (identifiers, types, imports and GraphQL documents are left alone), the
// translation catalogues and ingestion templates are rewritten as data, and the logo / wordmark /
// favicon / page title are replaced. Idempotent: running it twice changes nothing more.
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';

const [webDir, brandDir] = process.argv.slice(2).map((p) => path.resolve(p));
if (!webDir || !brandDir) {
    console.error('usage: node rebrand.mjs <web-ui-source-dir> <brand-dir>');
    process.exit(2);
}
const ts = createRequire(path.join(webDir, 'package.json'))('typescript');

const NAME = 'Cairn';
const DOCS = '/docs';

/** Rewrites one piece of user-visible text. `prose` also rewrites lowercase mentions. */
export function brand(text, { prose = false } = {}) {
    let s = text
        // Upstream documentation and project links → the Cairn docs root (paths would leak the name).
        .replace(/https?:\/\/(www\.)?(docs\.datahub\.com|datahubproject\.io|datahub\.(com|io))[^\s'"`)<>]*/g, DOCS)
        .replace(/https?:\/\/github\.com\/(datahub-project|acryldata)\/[^\s'"`)<>]*/g, DOCS)
        .replace(/\bdatahub\.(local|amazonaws\.company\.com)/g, 'cairn.$1')
        .replace(/assets\/logos\/acryl-[\w-]+\.svg/g, 'assets/brand/cairn-logo.svg')
        .replace(/DataHub Cloud/g, NAME)
        .replace(/Acryl Data|Acryl/g, NAME)
        .replace(/DataHub(?![A-Za-z0-9])/g, NAME)
        .replace(/Datahub(?![A-Za-z0-9])/g, NAME)
        .replace(/DATAHUB(?=[ .,!?:;)]|$)/g, NAME.toUpperCase());
    // Prose also covers inflected forms (e.g. Finnish "DataHubiin", German "DataHubs").
    if (prose) s = s.replace(/DataHub|Datahub/g, NAME).replace(/(?<![\w./:@-])datahub(?![\w-])/g, NAME.toLowerCase());
    return s;
}

const stats = { files: 0, literals: 0, json: 0, assets: 0 };

function* walk(dir) {
    for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
        const p = path.join(dir, e.name);
        if (e.isDirectory()) {
            if (['node_modules', '__tests__', 'graphql', 'dist'].includes(e.name)) continue;
            yield* walk(p);
        } else yield p;
    }
}

// ---- 1. TS/TSX: user-visible literals only --------------------------------------------------
function rewriteSource(file) {
    const src = fs.readFileSync(file, 'utf8');
    if (!/DataHub|Datahub|datahub\.com|datahubproject|DATAHUB|Acryl/.test(src)) return;
    const kind = file.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
    const sf = ts.createSourceFile(file, src, ts.ScriptTarget.Latest, true, kind);
    const edits = [];
    const visit = (node) => {
        // Never touch module specifiers or GraphQL documents (gql`...`).
        if (ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) return;
        if (ts.isTaggedTemplateExpression(node) && /gql|graphql/.test(node.tag.getText(sf))) return;
        let start;
        let end;
        let raw;
        let prose = false;
        if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
            start = node.getStart(sf) + 1;
            end = node.getEnd() - 1;
        } else if (ts.isTemplateHead(node) || ts.isTemplateMiddle(node) || ts.isTemplateTail(node)) {
            start = node.getStart(sf) + 1;
            end = node.getEnd() - (ts.isTemplateTail(node) ? 1 : 2);
        } else if (ts.isJsxText(node)) {
            start = node.getStart(sf);
            end = node.getEnd();
            prose = true;
        }
        if (start !== undefined) {
            raw = src.slice(start, end);
            // Identifier-like strings are keys (translation keys, storage keys, type names), not
            // text: leave them, except a literal that is exactly the product name.
            if (!prose && /^[A-Za-z0-9_.:-]+$/.test(raw) && raw !== 'DataHub' && !/^https?:/.test(raw)) {
                ts.forEachChild(node, visit);
                return;
            }
            const out = brand(raw, { prose });
            if (out !== raw) edits.push([start, end, out]);
        }
        ts.forEachChild(node, visit);
    };
    visit(sf);
    if (!edits.length) return;
    let out = src;
    for (const [s, e, text] of edits.sort((a, b) => b[0] - a[0])) out = out.slice(0, s) + text + out.slice(e);
    fs.writeFileSync(file, out);
    stats.files += 1;
    stats.literals += edits.length;
}

// ---- 2. JSON data: translations, ingestion templates, theme presets --------------------------
function rewriteJson(file) {
    const src = fs.readFileSync(file, 'utf8');
    if (!/DataHub|Datahub|datahub/.test(src)) return;
    const prose = file.includes(`${path.sep}i18n${path.sep}`);
    const map = (v) => (typeof v === 'string' ? brand(v, { prose }) : Array.isArray(v) ? v.map(map)
        : v && typeof v === 'object' ? Object.fromEntries(Object.entries(v).map(([k, x]) => [k, map(x)])) : v);
    const data = JSON.parse(src);
    const next = map(data);
    const text = `${JSON.stringify(next, null, file.includes('i18n') ? 4 : 2)}\n`;
    if (JSON.stringify(next) !== JSON.stringify(data)) {
        fs.writeFileSync(file, text);
        stats.json += 1;
    }
}

for (const file of walk(path.join(webDir, 'src'))) {
    if (/\.(test|spec)\.tsx?$/.test(file)) continue;
    if (/\.tsx?$/.test(file)) rewriteSource(file);
    else if (file.endsWith('.json')) rewriteJson(file);
}

// ---- 3. Panels whose content is the vendor's own media (release blog, product-tour videos,
//         screenshots): switched off. Each must still be found, so an upstream rename fails loudly.
const DISABLE = [
    ['src/app/shared/product/update/ProductUpdates.tsx', /export (default )?function (\w+)\(([^)]*)\)[^{]*\{/],
    ['src/app/onboarding/WelcomeToDataHubModal.tsx', /export const WelcomeToDataHubModal = \(\) => \{/],
    ['src/app/ingestV2/source/multiStepBuilder/CreateSourceEducationModal.tsx', /export default function CreateSourceEducationModal\(\) \{/],
];
for (const [rel, signature] of DISABLE) {
    const file = path.join(webDir, rel);
    const s = fs.readFileSync(file, 'utf8');
    const marker = '// cairn: vendor media panel disabled';
    if (s.includes(marker)) continue;
    const patched = s.replace(signature, (m) => `${m}\n    ${marker}\n    if (true as boolean) return null;`);
    if (patched === s) throw new Error(`${rel}: component signature not found`);
    fs.writeFileSync(file, patched);
}

// ---- 4. Brand assets and page shell -----------------------------------------------------------
const copy = (from, to) => {
    fs.copyFileSync(path.join(brandDir, from), path.join(webDir, to));
    stats.assets += 1;
};
copy('cairn-wordmark.svg', 'src/images/datahub_core.svg');
for (const f of fs.readdirSync(path.join(webDir, 'src/images'))) {
    if (/^datahub-logo.*\.svg$/.test(f)) copy('cairn-mark.svg', `src/images/${f}`);
}
copy('cairn-mark.svg', 'public/assets/logos/datahublogo.svg');
copy('logo512.png', 'public/assets/logo.png');
copy('favicon.ico', 'public/assets/icons/favicon.ico');
for (const f of ['logo192.png', 'logo512.png']) {
    if (fs.existsSync(path.join(webDir, 'public', f))) copy(f, `public/${f}`);
}
const indexHtml = path.join(webDir, 'index.html');
fs.writeFileSync(indexHtml, brand(fs.readFileSync(indexHtml, 'utf8'))
    .replace(/<meta name="description" content="[^"]*"/, '<meta name="description" content="Cairn: active metadata and data governance"')
    .replace(/\s*<!--[\s\S]*?-->/g, ''));
const manifest = path.join(webDir, 'public/manifest.json');
const m = JSON.parse(fs.readFileSync(manifest, 'utf8'));
m.short_name = NAME;
m.name = NAME;
fs.writeFileSync(manifest, `${JSON.stringify(m, null, 4)}\n`);

console.log(JSON.stringify(stats));
