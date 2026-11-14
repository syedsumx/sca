import { render } from '@testing-library/react';
import React from 'react';

import { forceLayout } from '../utils/forceLayout';
import { qualityLabel, relativeTime, shortUrn } from '../utils/format';
import { catalogEntityHref } from '../context/NativeLinks';
import { MarkdownView, extractMentions, isSafeUrl, tokenizeInline } from '../utils/markdown';
import { DS } from './mockApi';

describe('markdown', () => {
    it('never renders raw HTML or unsafe links', () => {
        const { container } = render(
            <MarkdownView source={'<img src=x onerror=alert(1)> **bold** [x](javascript:alert(1)) [ok](https://cairn.example.com)'} />,
        );
        expect(container.querySelector('img')).toBeNull();
        expect(container.textContent).toContain('<img src=x onerror=alert(1)>');
        expect(container.querySelector('strong')?.textContent).toBe('bold');
        const links = Array.from(container.querySelectorAll('a'));
        expect(links.map((a) => a.getAttribute('href'))).toEqual(['https://cairn.example.com']);
        expect(isSafeUrl('javascript:alert(1)')).toBe(false);
        expect(isSafeUrl('#/asset/x')).toBe(true);
        expect(isSafeUrl('//evil.example.com/x')).toBe(false);
        expect(isSafeUrl('/cairn/asset/x')).toBe(true);
    });

    it('renders lists, code, mentions and urn links', () => {
        const { container } = render(<MarkdownView source={`## Title\n- one\n- two @chen\n\n\`\`\`\nSELECT 1\n\`\`\`\nSee ${DS}`} />);
        expect(container.querySelectorAll('li')).toHaveLength(2);
        expect(container.querySelector('pre code')?.textContent).toBe('SELECT 1');
        expect(container.querySelector('.cairn-mention')?.textContent).toBe('@chen');
        const urnLink = container.querySelector('a.cairn-urn');
        expect(urnLink?.getAttribute('href')).toBe(`#/asset/${encodeURIComponent(DS)}`);
        expect(urnLink?.textContent).toBe('analytics.public.orders');
        expect(extractMentions('hi @chen and @emeka.o @chen')).toEqual(['chen', 'emeka.o']);
        expect(tokenizeInline('a `b` c').map((t) => t.kind)).toEqual(['text', 'code', 'text']);
    });
});

describe('format helpers', () => {
    it('shortUrn, relativeTime, qualityLabel', () => {
        expect(shortUrn(DS)).toBe('orders');
        const now = Date.parse('2026-10-25T12:00:00Z');
        expect(relativeTime('2026-10-25T11:58:00Z', now)).toBe('2 min ago');
        expect(relativeTime('2026-10-24T12:00:00Z', now)).toBe('1 d ago');
        expect(qualityLabel('FAIL')).toBe('Failing');
        // cairn-api trust badges use these spellings (services.trust / embed _status)
        expect(qualityLabel('FAILING')).toBe('Failing');
        expect(qualityLabel('WARNING')).toBe('Warning');
        expect(qualityLabel('LEARNING')).toBe('Learning');
        expect(qualityLabel(null)).toBe('Untested');
    });
});

describe('forceLayout', () => {
    it('is deterministic and keeps nodes inside the canvas', () => {
        const nodes = ['a', 'b', 'c', 'd'].map((id) => ({ id }));
        const edges = [
            { source: 'a', target: 'b' },
            { source: 'b', target: 'c' },
        ];
        const p1 = forceLayout(nodes, edges, { width: 400, height: 300 });
        const p2 = forceLayout(nodes, edges, { width: 400, height: 300 });
        expect(p1).toEqual(p2);
        Object.values(p1).forEach((p) => {
            expect(p.x).toBeGreaterThanOrEqual(40);
            expect(p.x).toBeLessThanOrEqual(360);
            expect(p.y).toBeGreaterThanOrEqual(30);
            expect(p.y).toBeLessThanOrEqual(270);
        });
        const d = (u: string, v: string) => Math.hypot(p1[u].x - p1[v].x, p1[u].y - p1[v].y);
        expect(d('a', 'b')).toBeLessThan(d('a', 'd') + 1); // connected nodes pulled together
    });
});

describe('catalogEntityHref', () => {
    it('uses the entity-specific Cairn Core route', () => {
        expect(catalogEntityHref('', 'urn:li:dashboard:(looker,rev)')).toBe('/dashboard/urn%3Ali%3Adashboard%3A(looker%2Crev)');
        expect(catalogEntityHref('https://dh.example.com/', 'urn:li:dataset:(a,b,PROD)')).toMatch(/^https:\/\/dh\.example\.com\/dataset\//);
        expect(catalogEntityHref('', 'urn:li:query:abc')).toBeNull();
    });
});
