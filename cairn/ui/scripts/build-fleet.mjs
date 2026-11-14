// Builds the vendor's fleet console (L161) as one self-contained page: dist/fleet/index.html, served by
// `cairn fleet serve` (CAIRN_FLEET_UI_DIR), and dist/fleet-demo/index.html, the same page on sample data for a preview.
import { build } from 'esbuild';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const antdCss = readFileSync(join(root, 'node_modules/antd/dist/antd.min.css'), 'utf8');
const shell = readFileSync(join(root, 'demo/shell.html'), 'utf8').replace('<title>Cairn Workspace</title>', '<title>Cairn fleet</title>');
const banner = /<div class="demo-banner"[\s\S]*?<\/div>\n<script>[\s\S]*?<\/script>\n/;
if (!banner.test(shell)) throw new Error('demo/shell.html: the banner block changed; update build-fleet.mjs');

for (const demo of [false, true]) {
    const result = await build({
        entryPoints: [join(root, 'fleet/main.tsx')],
        bundle: true, minify: true, format: 'iife', target: 'es2019', write: false, legalComments: 'none',
        define: { 'process.env.NODE_ENV': '"production"', FLEET_DEMO: String(demo) }, jsx: 'transform',
    });
    const js = result.outputFiles[0].text.replace(/<\/script/gi, '<\\/script');
    let page = shell;
    page = demo
        ? page.replace(/<strong>Cairn demo<\/strong>\n  <span[^>]*>[^<]*<\/span>/,
            '<strong>Cairn fleet demo</strong>\n  <span>The vendor\'s console on sample data: fictional customers\' installs. Changes last until you reload.</span>')
        : page.replace(banner, '');
    const html = page.replace('/*ANTD_CSS*/', () => antdCss).replace('/*APP_JS*/', () => js);
    const out = join(root, demo ? 'dist/fleet-demo' : 'dist/fleet');
    mkdirSync(out, { recursive: true });
    writeFileSync(join(out, 'index.html'), html);
    console.log(`${demo ? 'dist/fleet-demo' : 'dist/fleet'}/index.html: ${(html.length / 1024).toFixed(0)} kB`);
}
