import { render } from '@testing-library/react';
import { Select } from 'antd';
import fs from 'fs';
import path from 'path';
import React from 'react';

// eslint-disable-next-line @typescript-eslint/no-var-requires
const { FIXES } = require('../../../scripts/rc-select-a11y-loader.js') as { FIXES: [string, string][] };

describe('antd Select ARIA (the build-time correction of rc-select)', () => {
    it('a closed Select says it is collapsed and points at no list that is not there', () => {
        const { container } = render(<Select aria-label="Kind" options={[{ value: 'a', label: 'A' }]} />);
        const input = container.querySelector('input[role="combobox"]') as HTMLInputElement;
        expect(input).not.toBeNull();
        expect(input.getAttribute('aria-expanded')).toBe('false');
        expect(input.hasAttribute('aria-owns')).toBe(false);
        expect(input.hasAttribute('aria-controls')).toBe(false);
        expect(input.hasAttribute('aria-activedescendant')).toBe(false);
    });

    it('every fix still matches the installed rc-select (es and lib)', () => {
        const base = path.dirname(require.resolve('rc-select/package.json'));
        for (const flavour of ['es', 'lib']) {
            const src = fs.readFileSync(path.join(base, flavour, 'Selector', 'Input.js'), 'utf8');
            for (const [from] of FIXES) expect(src.includes(from)).toBe(true);
        }
    });
});
