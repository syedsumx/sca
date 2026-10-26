/* eslint-disable */
// Module-Federation remote for Cairn Core's MFE host (cairn-web-ui/src/app/mfeframework).
// The host loads remotes with { format: 'var', from: 'webpack' } and calls mount(el, {}), sharing no
// dependencies, so this bundle is self-contained (own React 17 + antd).
//   npm run build:mfe  →  dist/mfe/remoteEntry.js  (+ hashed chunks)
const path = require('path');
const { ModuleFederationPlugin } = require('webpack').container;

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
        ],
        optimization: {
            splitChunks: { chunks: 'async' },
        },
        performance: { hints: production ? 'warning' : false, maxAssetSize: 600000, maxEntrypointSize: 600000 },
        stats: 'errors-warnings',
    };
};
