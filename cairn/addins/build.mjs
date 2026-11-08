// Builds the spreadsheet add-ins for one Cairn into dist/ (no dependencies).
//
//   node build.mjs --cairn-url https://cairn.example.com --addin-url https://addins.example.com
//                  [--api-base https://cairn.example.com/cairn/api] [--id <GUID>] [--out dist]
//
// dist/excel: the Office add-in's files, to serve from --addin-url over https (any static host), and its
//             manifest.xml, to sideload or deploy from the Microsoft 365 admin center.
// dist/sheets: the Apps Script project (push it with clasp, or paste the files into the script editor).
import { cpSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const opt = (name) => {
    const i = args.indexOf(`--${name}`);
    return i >= 0 ? args[i + 1] : undefined;
};
const origin = (value, name) => {
    let u;
    try {
        u = new URL(value);
    } catch {
        throw new Error(`--${name}: not a URL: ${value}`);
    }
    const loopback = u.protocol === 'http:' && ['localhost', '127.0.0.1'].includes(u.hostname);
    if (u.protocol !== 'https:' && !loopback) throw new Error(`--${name} must be https`);
    return u;
};

const cairn = origin(opt('cairn-url') || 'https://cairn.example.com', 'cairn-url');
const addin = origin(opt('addin-url') || 'https://addins.example.com', 'addin-url');
const apiBase = (opt('api-base') || `${cairn.origin}${cairn.pathname.replace(/\/+$/, '')}/cairn/api`).replace(/\/+$/, '');
origin(apiBase, 'api-base');
const addinBase = `${addin.origin}${addin.pathname.replace(/\/+$/, '')}`;
const id = opt('id');
if (id && !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id)) throw new Error('--id: a GUID');
const out = opt('out') || join(here, 'dist');

const replaceAll = (text) => text.split('https://addins.example.com').join(addinBase)
    .split('https://cairn.example.com').join(cairn.origin);

rmSync(out, { recursive: true, force: true });
// ---- Excel ----
const excel = join(out, 'excel');
mkdirSync(excel, { recursive: true });
cpSync(join(here, 'excel', 'src'), excel, { recursive: true });
for (const f of ['functions.json', 'taskpane.html', 'help.html']) {
    writeFileSync(join(excel, f), replaceAll(readFileSync(join(excel, f), 'utf8')));
}
let manifest = replaceAll(readFileSync(join(here, 'excel', 'manifest.xml'), 'utf8'));
if (id) manifest = manifest.replace(/<Id>[^<]*<\/Id>/, `<Id>${id}</Id>`);
writeFileSync(join(excel, 'manifest.xml'), manifest);
writeFileSync(join(excel, 'config.js'), `/* Built for ${cairn.origin}. */\nwindow.CAIRN_ADDIN_CONFIG = ${JSON.stringify({
    apiBase, clientId: 'agt_excel_addin', redirectUri: `${addinBase}/callback.html`,
}, null, 4)};\n`);
// ---- Google Sheets ----
const sheets = join(out, 'sheets');
mkdirSync(sheets, { recursive: true });
for (const f of readdirSync(join(here, 'sheets'))) cpSync(join(here, 'sheets', f), join(sheets, f));
const project = JSON.parse(readFileSync(join(sheets, 'appsscript.json'), 'utf8'));
project.urlFetchWhitelist = [`${new URL(apiBase).origin}/`];
writeFileSync(join(sheets, 'appsscript.json'), `${JSON.stringify(project, null, 2)}\n`);
console.log(`built the add-ins for ${apiBase}: ${excel} (serve from ${addinBase}), ${sheets} (script property CAIRN_API_URL=${apiBase})`);
