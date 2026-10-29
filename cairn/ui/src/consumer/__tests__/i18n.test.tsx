import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { ConsumerLayout } from '../components/Layout';
import { CairnProvider } from '../context/CairnProvider';
import { I18nProvider, detectLocale, translate } from '../i18n';
import { CATALOGS, SOURCE } from '../i18n/messages';
import { UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);
afterEach(() => window.localStorage.clear());

function layout(initial?: 'en' | 'de' | 'ja') {
    const { client } = makeClient({ 'GET /ui/config': UI_CONFIG, 'GET /notify/inbox': [] });
    render(
        <MemoryRouter>
            <I18nProvider initial={initial}>
                <CairnProvider client={client} cache={new QueryCache()}>
                    <ConsumerLayout />
                </CairnProvider>
            </I18nProvider>
        </MemoryRouter>,
    );
}

it('every translation uses the same keys and placeholders as English', () => {
    for (const [locale, catalog] of Object.entries(CATALOGS)) {
        for (const key of Object.keys(catalog)) expect(SOURCE, `${locale}: ${key}`).toHaveProperty(key);
        for (const [key, text] of Object.entries(catalog)) {
            const vars = (s: string) => (s.match(/\{\w+\}/g) ?? []).sort().join();
            expect(vars(text as string), `${locale}: ${key}`).toBe(vars(SOURCE[key as keyof typeof SOURCE]));
        }
    }
    expect(Object.keys(CATALOGS.de)).toHaveLength(Object.keys(SOURCE).length); // complete, not just started
});

it('falls back to English, then the key', () => {
    expect(translate('de', 'nav.search')).toBe('Suche');
    expect(translate('fr', 'nav.search')).toBe('Recherche');
    expect(translate('de', 'missing.key' as never)).toBe('missing.key');
});

it('uses the saved choice, else the browser language', () => {
    expect(detectLocale()).toBe('en');
    window.localStorage.setItem('cairn.locale', 'ja');
    expect(detectLocale()).toBe('ja');
    window.localStorage.setItem('cairn.locale', 'xx');
    expect(detectLocale()).toBe('en');
});

it('shows the navigation in the chosen language and remembers a switch', async () => {
    layout('de');
    expect(await screen.findByText('Entdecken')).toBeTruthy();
    expect(screen.getByRole('navigation', { name: 'Bereiche' })).toBeTruthy();
    expect(document.documentElement.lang).toBe('de');
    fireEvent.mouseDown(screen.getByRole('combobox', { name: 'Sprache' }));
    fireEvent.click(await screen.findByText('日本語'));
    await waitFor(() => expect(screen.getByText('発見')).toBeTruthy());
    expect(window.localStorage.getItem('cairn.locale')).toBe('ja');
});

it('stays English by default', async () => {
    layout();
    expect(await screen.findByText('Discover')).toBeTruthy();
    expect(screen.getByRole('button', { name: '✨ Ask' })).toBeTruthy();
});
