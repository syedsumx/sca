// Builds the unpacked extension into dist/ (load it from chrome://extensions, or zip it for the
// Chrome Web Store or managed deployment).
//
//   node build.mjs [--cairn-url https://cairn.example.com] [--out dist]
//
// --cairn-url bakes in the Cairn address and its host permission, so people do not have to type it
// (the way to roll the extension out to a company).
import { cpSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const opt = (name) => {
    const i = args.indexOf(`--${name}`);
    return i >= 0 ? args[i + 1] : undefined;
};
const out = opt('out') || join(here, 'dist');
const cairnUrl = (opt('cairn-url') || '').replace(/\/+$/, '');

rmSync(out, { recursive: true, force: true });
mkdirSync(out, { recursive: true });
cpSync(join(here, 'src'), out, { recursive: true });
const manifest = JSON.parse(readFileSync(join(here, 'src', 'manifest.json'), 'utf8'));
if (cairnUrl) {
    manifest.host_permissions.push(`${new URL(cairnUrl).origin}/*`);
}
writeFileSync(join(out, 'manifest.json'), `${JSON.stringify(manifest, null, 2)}\n`);
writeFileSync(join(out, 'defaults.json'), `${JSON.stringify({ cairnUrl })}\n`);
console.log(`built ${manifest.name} ${manifest.version} → ${out}${cairnUrl ? ` (Cairn: ${cairnUrl})` : ''}`);
