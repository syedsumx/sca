// Builds dist/demo/index.html: the consumer workspace as one self-contained page backed by the
// in-browser demo API (demo/demoApi.ts). Used for click-through previews without a DataHub stack.
import { build } from 'esbuild';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const out = join(root, 'dist/demo');
mkdirSync(out, { recursive: true });

const result = await build({
    entryPoints: [join(root, 'demo/main.tsx')],
    bundle: true, minify: true, format: 'iife', target: 'es2019', write: false, legalComments: 'none',
    define: { 'process.env.NODE_ENV': '"production"' }, loader: { '.svg': 'dataurl', '.png': 'dataurl' },
    jsx: 'transform',
});
const js = result.outputFiles[0].text.replace(/<\/script/gi, '<\\/script');
const antdCss = readFileSync(join(root, 'node_modules/antd/dist/antd.min.css'), 'utf8');
const shell = readFileSync(join(root, 'demo/shell.html'), 'utf8');
const html = shell.replace('/*ANTD_CSS*/', () => antdCss).replace('/*APP_JS*/', () => js);
writeFileSync(join(out, 'index.html'), html);
console.log(`dist/demo/index.html: ${(html.length / 1024).toFixed(0)} kB`);
