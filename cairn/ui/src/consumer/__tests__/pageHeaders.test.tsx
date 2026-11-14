/**
 * The purpose-built pages use the one page header (title, description, primary action, a "?" that opens the docs for
 * the page) rather than headers of their own, and each names a docs section that the bundled help has.
 */
import { render, screen } from '@testing-library/react';
import { readFileSync } from 'fs';
import { join } from 'path';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { PageHeader } from '../guide/PageHeader';
import HELP from '../guide/help.generated.json';
import { pageHelp } from '../guide/help';

const PAGES: [string, string, string[]][] = [
    // [file, the component's route, routes whose header it draws]
    ['DiscoverPage.tsx', '/discover', ['/discover']],
    ['AssetPage.tsx', '/asset', ['/asset']],
    ['MarketplacePages.tsx', '/requests', ['/requests']],
    ['FlowsPage.tsx', '/flows', ['/flows']],
    ['OntologyPage.tsx', '/ontology', ['/ontology']],
    ['InboxPage.tsx', '/inbox', ['/inbox']],
    ['ProfilePage.tsx', '/profile', ['/profile']],
    ['SettingsPage.tsx', '/settings', ['/settings']],
    ['PeoplePage.tsx', '/people', ['/people']],
    ['ObservabilityPage.tsx', '/observability', ['/observability']],
];

describe('the shared page header on the purpose-built pages', () => {
    it.each(PAGES)('%s draws the shared header, not one of its own', (file, _route, routes) => {
        const src = readFileSync(join(__dirname, '..', 'pages', file), 'utf8');
        expect(src).toMatch(/<PageHeader\b/);
        expect(src).not.toMatch(/<h1\b|as="h1"/);
        for (const r of routes) expect(src).toContain(`help={pageHelp('${r}')}`);
    });

    it('every page’s help names a docs section that the bundled help has', () => {
        const help = HELP as Record<string, unknown>;
        for (const r of ['/discover', '/asset', '/inbox', '/requests', '/flows', '/ontology', '/profile', '/settings', '/people', '/observability']) {
            const anchor = pageHelp(r);
            expect(anchor, r).toMatch(/^[\w-]+\.html#[\w-]+$/);
            expect(help[anchor as string], `${r}: ${anchor}`).toBeTruthy();
        }
    });

    it('the header has the title, the description, the help button and the docs anchor', () => {
        render(
            <MemoryRouter>
                <PageHeader title="Flows" description="Orchestrate" help={pageHelp('/flows')} actions={<button type="button">New flow</button>} />
            </MemoryRouter>,
        );
        expect(screen.getByRole('heading', { level: 1, name: 'Flows' })).toBeTruthy();
        expect(screen.getByText('Orchestrate')).toBeTruthy();
        expect(screen.getByRole('button', { name: 'New flow' })).toBeTruthy();
        const help = screen.getByRole('button', { name: /help/i });
        expect(help.getAttribute('aria-haspopup')).toBe('dialog');
        expect(help.closest('[data-page-header]')?.getAttribute('data-help-anchor')).toBe(pageHelp('/flows'));
    });
});
