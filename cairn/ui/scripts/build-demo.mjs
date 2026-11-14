// Builds dist/demo/index.html: the consumer workspace as one self-contained page backed by the
// in-browser demo API (demo/demoApi.ts). Used for click-through previews without a Cairn Core stack.
import { build } from 'esbuild';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const out = join(root, 'dist/demo');
mkdirSync(out, { recursive: true });

// The same ARIA correction the MFE build makes to antd 4's Select (scripts/rc-select-a11y-loader.js): without it every
// closed Select's combobox lacks aria-expanded and points at a listbox that is not rendered.
const rcSelectA11y = createRequire(import.meta.url)('./rc-select-a11y-loader.js');
let a11yFixed = 0;
const a11yPlugin = {
    name: 'rc-select-a11y',
    setup(b) {
        b.onLoad({ filter: /rc-select[\\/](es|lib)[\\/]Selector[\\/]Input\.js$/ }, (args) => {
            a11yFixed += 1;
            return { contents: rcSelectA11y.call({ resourcePath: args.path }, readFileSync(args.path, 'utf8')), loader: 'js' };
        });
    },
};

// The hosted demo site has no roadmap: the platform map page (and capabilities.json, generated from ROADMAP.md) is
// replaced by an empty module, and __CAIRN_NO_PLATFORM_MAP__ takes its sidebar entry and route out.
let platformMapLeftOut = 0;
const noPlatformMap = {
    name: 'no-platform-map',
    setup(b) {
        b.onResolve({ filter: /[\\/]resources[\\/]CapabilitiesPage$/ }, () => {
            platformMapLeftOut += 1;
            return { path: 'CapabilitiesPage', namespace: 'no-platform-map' };
        });
        b.onLoad({ filter: /.*/, namespace: 'no-platform-map' }, () => ({
            contents: 'export function CapabilitiesPage() { return null; }', loader: 'js',
        }));
    },
};

const result = await build({
    entryPoints: [join(root, 'demo/main.tsx')],
    bundle: true, minify: true, format: 'iife', target: 'es2019', write: false, legalComments: 'none',
    define: { 'process.env.NODE_ENV': '"production"', __CAIRN_NO_PLATFORM_MAP__: 'true' }, loader: { '.svg': 'dataurl', '.png': 'dataurl' },
    jsx: 'transform', plugins: [a11yPlugin, noPlatformMap],
});
if (!a11yFixed) throw new Error('rc-select-a11y: the Select input module was not bundled; review the fix');
if (!platformMapLeftOut) throw new Error('no-platform-map: the platform map page was not found to leave out; review the plugin');
const js = result.outputFiles[0].text.replace(/<\/script/gi, '<\\/script');
const antdCss = readFileSync(join(root, 'node_modules/antd/dist/antd.min.css'), 'utf8');
const shell = readFileSync(join(root, 'demo/shell.html'), 'utf8');
const html = shell.replace('/*ANTD_CSS*/', () => antdCss).replace('/*APP_JS*/', () => js);
// The roadmap must not be in the page: none of its rows' notes or summaries (the generated data, from ROADMAP.md).
const roadmap = JSON.parse(readFileSync(join(root, 'src/consumer/resources/capabilities.json'), 'utf8'));
const markers = roadmap.items.flatMap((r) => [r.note, r.summary]).filter((t) => typeof t === 'string' && t.length > 80)
    .map((t) => JSON.stringify(t).slice(1, 61));
if (markers.length < 20) throw new Error('no-platform-map: too few roadmap rows to check the page against');
const found = markers.find((m) => html.includes(m));
if (found) throw new Error(`the demo page still carries the roadmap (${found})`);
writeFileSync(join(out, 'index.html'), html);
console.log(`dist/demo/index.html: ${(html.length / 1024).toFixed(0)} kB`);
