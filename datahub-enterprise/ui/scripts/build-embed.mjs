// Builds the dependency-free embed SDK: dist/embed/sdk.js and dist/embed/frame.js (IIFE, ES2019).
import { build } from 'esbuild';
import { gzipSync } from 'node:zlib';
import { readFileSync, mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const out = join(root, 'dist/embed');
mkdirSync(out, { recursive: true });

const common = { bundle: true, minify: true, format: 'iife', target: 'es2019', sourcemap: true, legalComments: 'none' };
await build({ ...common, entryPoints: [join(root, 'src/embed/index.ts')], outfile: join(out, 'sdk.js'), globalName: 'DheEmbed' });
await build({ ...common, entryPoints: [join(root, 'src/embed/frame.ts')], outfile: join(out, 'frame.js') });

writeFileSync(
    join(out, 'frame.html'),
    `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DataHub trust</title><style>html,body{margin:0;padding:8px;background:transparent}</style></head>
<body><div id="dhe-embed"></div><script src="./frame.js"></script></body></html>
`,
);

for (const f of ['sdk.js', 'frame.js']) {
    const buf = readFileSync(join(out, f));
    console.log(`${f}: ${buf.length} B raw, ${gzipSync(buf, { level: 9 }).length} B gz`);
}
