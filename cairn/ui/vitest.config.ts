import { defineConfig } from 'vitest/config';

export default defineConfig({
    test: {
        environment: 'jsdom',
        globals: true,
        include: ['src/**/*.test.{ts,tsx}'],
        // antd 4's Select renders with the same ARIA correction the bundles make (scripts/rc-select-a11y-loader.js).
        setupFiles: ['./scripts/rc-select-a11y-require.cjs'],
    },
});
