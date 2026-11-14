/* eslint-disable */
// Module-Federation remote for Cairn Core's MFE host (cairn-web-ui/src/app/mfeframework).
// The host loads remotes with { format: 'var', from: 'webpack' } and calls mount(el, {}), sharing no
// dependencies, so this bundle is self-contained (own React 17 + antd).
//   npm run build:mfe  →  dist/mfe/remoteEntry.js  (+ hashed chunks)
const fs = require('fs');
const path = require('path');
const webpack = require('webpack');
const { ModuleFederationPlugin } = webpack.container;

/** The installable app (ui/pwa): its shell page, manifest, icons and service worker, next to remoteEntry.js. The
 *  service worker's cache is named after the build, so a deploy replaces the cached shell. */
class PwaShellPlugin {
    apply(compiler) {
        compiler.hooks.thisCompilation.tap('PwaShellPlugin', (compilation) => {
            compilation.hooks.processAssets.tap({ name: 'PwaShellPlugin', stage: webpack.Compilation.PROCESS_ASSETS_STAGE_REPORT }, () => {
                const dir = path.resolve(__dirname, 'pwa');
                const walk = (rel) => fs.readdirSync(path.join(dir, rel), { withFileTypes: true }).flatMap((e) =>
                    e.isDirectory() ? walk(path.join(rel, e.name)) : [path.join(rel, e.name)]);
                for (const rel of walk('')) {
                    let buf = fs.readFileSync(path.join(dir, rel));
                    if (rel === 'sw.js') buf = Buffer.from(buf.toString('utf8').replace('__CAIRN_BUILD__', compilation.hash.slice(0, 12)));
                    compilation.emitAsset(rel.split(path.sep).join('/'), new webpack.sources.RawSource(buf));
                }
                // The full page has no catalog host to bring antd's styles: the app shell links its own copy.
                compilation.emitAsset('antd.min.css', new webpack.sources.RawSource(
                    fs.readFileSync(require.resolve('antd/dist/antd.min.css'))));
            });
        });
    }
}

module.exports = (env = {}, argv = {}) => {
    const production = argv.mode !== 'development';
    return {
        mode: production ? 'production' : 'development',
        entry: {},
        devtool: production ? 'source-map' : 'eval-cheap-module-source-map',
        output: {
            path: path.resolve(__dirname, 'dist/mfe'),
            // Chunks are fetched relative to remoteEntry.js wherever it is served (e.g. /cairn-ui/).
            publicPath: 'auto',
            filename: '[name].[contenthash:8].js',
            chunkFilename: '[name].[contenthash:8].js',
            uniqueName: 'cairnConsumer',
            clean: true,
        },
        resolve: { extensions: ['.tsx', '.ts', '.js'] },
        module: {
            rules: [
                {
                    test: /\.[jt]sx?$/,
                    exclude: /node_modules/,
                    loader: 'esbuild-loader',
                    options: { target: 'es2019', jsx: 'automatic', tsconfig: path.resolve(__dirname, 'tsconfig.json') },
                },
                // Accessible ARIA on antd 4's Select (see the loader).
                {
                    test: /rc-select[\\/](es|lib)[\\/]Selector[\\/]Input\.js$/,
                    loader: path.resolve(__dirname, 'scripts/rc-select-a11y-loader.js'),
                },
                // antd 4 ships CSS-in-less; we rely on the host's antd styles being present, and
                // otherwise fall back to unstyled-but-functional components (no CSS loaders needed).
            ],
        },
        plugins: [
            new ModuleFederationPlugin({
                name: 'cairnConsumer',
                filename: 'remoteEntry.js',
                library: { type: 'var', name: 'cairnConsumer' },
                exposes: { './mount': './src/consumer/mount.tsx' },
                shared: {},
            }),
            new PwaShellPlugin(),
        ],
        optimization: {
            splitChunks: { chunks: 'async' },
        },
        performance: { hints: production ? 'warning' : false, maxAssetSize: 600000, maxEntrypointSize: 600000 },
        stats: 'errors-warnings',
    };
};
