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

const result = await build({
    entryPoints: [join(root, 'demo/main.tsx')],
    bundle: true, minify: true, format: 'iife', target: 'es2019', write: false, legalComments: 'none',
    define: { 'process.env.NODE_ENV': '"production"' }, loader: { '.svg': 'dataurl', '.png': 'dataurl' },
    jsx: 'transform', plugins: [a11yPlugin],
});
if (!a11yFixed) throw new Error('rc-select-a11y: the Select input module was not bundled; review the fix');
const js = result.outputFiles[0].text.replace(/<\/script/gi, '<\\/script');
const antdCss = readFileSync(join(root, 'node_modules/antd/dist/antd.min.css'), 'utf8');
const shell = readFileSync(join(root, 'demo/shell.html'), 'utf8');
const html = shell.replace('/*ANTD_CSS*/', () => antdCss).replace('/*APP_JS*/', () => js);
writeFileSync(join(out, 'index.html'), html);
console.log(`dist/demo/index.html: ${(html.length / 1024).toFixed(0)} kB`);
