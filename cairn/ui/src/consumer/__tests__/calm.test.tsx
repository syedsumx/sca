/**
 * L150, a calmer and consistent workspace: services that are not connected yet, one status vocabulary, row menus with
 * a confirmation, schedules and times in words, and one primary colour.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { CairnApiError } from '../../api/client';
import { ConsumerApp } from '../ConsumerApp';
import { ConsumerLayout } from '../components/Layout';
import { ErrorNotice } from '../components/ErrorNotice';
import { ApiDisclosure, RowActions } from '../components/RowActions';
import { StatusBadge, toneOf } from '../components/StatusBadge';
import { PRIMARY_CSS, tokens } from '../components/tokens';
import { CairnProvider } from '../context/CairnProvider';
import { I18nProvider, translate } from '../i18n';
import { formatValue } from '../resources/format';
import { describeError, isNotConnected, presentError, serviceOf } from '../utils/errors';
import { Schedule, When, cronText, looksLikeCron, relativeText } from '../utils/time';
import { UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const t = (key: Parameters<typeof translate>[1], vars?: Record<string, string | number>) => translate('en', key, vars);

function wrap(ui: React.ReactElement, locale: 'en' | 'de' = 'en') {
    const { client, cache } = makeClient({ 'GET /ui/config': UI_CONFIG, 'GET /notify/inbox': [] });
    return render(
        <MemoryRouter>
            <I18nProvider initial={locale}>
                <CairnProvider client={client} cache={cache}>{ui}</CairnProvider>
            </I18nProvider>
        </MemoryRouter>,
    );
}

const unmounted = (path: string) => new CairnApiError('HTTP 404', 404, path, { detail: 'Not Found' });

describe('a service that is not connected yet', () => {
    it('an unmounted API (FastAPI’s own 404) is "not connected", named after its service', () => {
        const p = presentError(unmounted('/v1/airflow/deployments'), t);
        expect(p.kind).toBe('notConnected');
        expect(p.title).toBe('Airflow isn’t connected yet');
        expect(p.connect?.to).toBe('/docs/administration.html#managed-airflow');
        expect(presentError(unmounted('/v1/observability/health'), t).title).toBe('Platform observability isn’t connected yet');
        expect(presentError(unmounted('/v1/people/users'), t).title).toBe('The people directory isn’t connected yet');
        expect(serviceOf('/cairn/api/v1/ml/monitors?x=1')?.name).toBe('svc.mlflow');
    });

    it('a real missing object stays "not found"', () => {
        const gone = new CairnApiError('HTTP 404', 404, '/v1/sql-workflows/wf_9', { detail: 'no such workflow' });
        expect(isNotConnected(gone)).toBe(false);
        expect(presentError(gone, t).kind).toBe('notFound');
        expect(presentError(gone, t).title).toBe('It was not found');
    });

    it('a refusal is still a refusal (fail closed is unchanged)', () => {
        const refused = new CairnApiError('HTTP 403', 403, '/v1/people/users', { detail: 'needs MANAGE_USERS' });
        expect(presentError(refused, t).kind).toBe('forbidden');
    });

    it('an unknown service uses the area’s name', () => {
        expect(presentError(unmounted('/v1/somewhere'), t, { area: 'Widgets' }).title).toBe('Widgets isn’t connected yet');
        expect(describeError(unmounted('/v1/airflow/dags'))).toMatch(/^Airflow isn’t connected yet\. /);
    });

    it('is shown as a calm, neutral state with where to connect it, not a red error', () => {
        wrap(<ErrorNotice error={unmounted('/v1/airflow/deployments')} />);
        const box = screen.getByRole('status');
        expect(box.getAttribute('data-error-kind')).toBe('notConnected');
        expect(within(box).getByText('Airflow isn’t connected yet')).toBeTruthy();
        expect(within(box).getByRole('link', { name: 'How to connect it' }).getAttribute('href'))
            .toBe('/docs/administration.html#managed-airflow');
        expect(document.querySelector('.ant-alert-error')).toBeNull();
    });
});

describe('one status vocabulary', () => {
    it('maps every way the APIs name a state to one word and one tone', () => {
        const cases: [string, string, string][] = [
            ['SUCCESS', 'Succeeded', 'ok'], ['success', 'Succeeded', 'ok'], ['succeeded', 'Succeeded', 'ok'],
            ['FAILURE', 'Failed', 'error'], ['failed', 'Failed', 'error'], ['error', 'Failed', 'error'],
            ['running', 'Running', 'running'], ['PENDING', 'Pending', 'waiting'], ['healthy', 'Healthy', 'ok'],
            ['enabled', 'On', 'ok'], ['disabled', 'Off', 'waiting'], ['sla_missed', 'SLA missed', 'warn'],
        ];
        wrap(<div>{cases.map(([s]) => <StatusBadge key={s} state={s} />)}</div>);
        for (const [state, word, tone] of cases) {
            const el = document.querySelector(`[data-state="${state}"]`) as HTMLElement;
            expect(el.textContent).toBe(word);
            expect(el.getAttribute('data-tone')).toBe(tone);
            expect(toneOf(state)).toBe(tone);
        }
    });

    it('in German too', () => {
        wrap(<StatusBadge state="FAILURE" />, 'de');
        expect(screen.getByText('Fehlgeschlagen')).toBeTruthy();
    });

    it('generic tables show states and on/off settings as badges, kinds as tags', () => {
        wrap(<div>
            <span data-testid="s">{formatValue('status', 'SUCCESS')}</span>
            <span data-testid="e">{formatValue('enabled', true)}</span>
            <span data-testid="k">{formatValue('kind', 'email')}</span>
        </div>);
        expect(screen.getByTestId('s').textContent).toBe('Succeeded');
        expect(screen.getByTestId('e').textContent).toBe('On');
        expect(screen.getByTestId('k').querySelector('.ant-tag')?.textContent).toBe('email');
    });
});

describe('row actions', () => {
    it('keeps the main action visible and asks before a delete in the menu', async () => {
        const deleted = vi.fn();
        const edited = vi.fn();
        wrap(<RowActions name="orders_fresh" primary={[{ key: 'edit', label: 'Edit', onSelect: edited }]}
            actions={[{ key: 'delete', label: 'Delete', danger: true, onSelect: deleted }]} />);
        fireEvent.click(screen.getByRole('button', { name: 'Edit: orders_fresh' }));
        expect(edited).toHaveBeenCalled();
        expect(screen.queryByRole('button', { name: /^Delete/ })).toBeNull();
        fireEvent.click(screen.getByRole('button', { name: 'More actions for orders_fresh' }));
        fireEvent.click(await screen.findByRole('menuitem', { name: 'Delete' }));
        const dialog = (await screen.findAllByRole('dialog')).slice(-1)[0];
        expect(within(dialog).getByText('Delete orders_fresh?')).toBeTruthy();
        expect(deleted).not.toHaveBeenCalled();
        fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
        await waitFor(() => expect(deleted).toHaveBeenCalledTimes(1));
    });

    it('cancel deletes nothing', async () => {
        const deleted = vi.fn();
        wrap(<RowActions name="raw" actions={[{ key: 'delete', label: 'Delete', danger: true, onSelect: deleted }]} />);
        fireEvent.click(screen.getByRole('button', { name: 'More actions for raw' }));
        fireEvent.click(await screen.findByRole('menuitem', { name: 'Delete' }));
        const dialog = (await screen.findAllByRole('dialog')).slice(-1)[0];
        fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
        expect(deleted).not.toHaveBeenCalled();
    });

    it('the API a page reads is behind a collapsed "API" disclosure', () => {
        wrap(<ApiDisclosure paths={['GET /quality/monitors']} />);
        const details = document.querySelector('details.cairn-api') as HTMLDetailsElement;
        expect(details.open).toBe(false);
        expect(details.querySelector('summary')?.textContent).toBe('API');
        expect(details.textContent).toContain('GET /quality/monitors');
    });
});

describe('schedules and times in words', () => {
    it('says common crons in words, with the time zone', () => {
        expect(cronText('0 */6 * * *', t)).toBe('Every 6 hours at minute 00 (UTC)');
        expect(cronText('0 2 * * *', t, 'en', 'Europe/London')).toBe('Every day at 02:00 Europe/London');
        expect(cronText('30 7 * * 1-5', t)).toBe('Every weekday at 07:30 UTC');
        expect(cronText('0 9 * * 1', t)).toBe('Every Monday at 09:00 UTC');
        expect(cronText('*/15 * * * *', t)).toBe('Every 15 minutes');
        expect(cronText('0 3 1 * *', t)).toBe('On day 1 of every month at 03:00 UTC');
    });

    it('falls back to the cron itself when it has no simple words', () => {
        expect(cronText('0 2 1 */3 *', t)).toBe('0 2 1 */3 *');
        expect(cronText('not a cron', t)).toBe('not a cron');
        expect(looksLikeCron('0 2 * * *')).toBe(true);
        expect(looksLikeCron('every day')).toBe(false);
    });

    it('shows the words with the cron on hover', () => {
        wrap(<Schedule cron="0 2 * * *" />);
        const el = screen.getByText('Every day at 02:00 UTC');
        expect(el.getAttribute('data-cron')).toBe('0 2 * * *');
    });

    it('says how long ago, with the exact time on hover', () => {
        const now = Date.parse('2026-11-11T12:00:00Z');
        expect(relativeText(now - 10_000, t, now)).toBe('just now');
        expect(relativeText(now - 5 * 60_000, t, now)).toBe('5 min ago');
        expect(relativeText(new Date(now - 5 * 3_600_000).toISOString(), t, now)).toBe('5 h ago');
        expect(relativeText(now - 3 * 86_400_000, t, now)).toBe('3 d ago');
        expect(relativeText((now - 2 * 3_600_000) / 1000, t, now)).toBe('2 h ago');   // epoch seconds
        expect(relativeText(now + 2 * 3_600_000, t, now)).toBe('in 2 h');
        expect(relativeText(null, t, now)).toBe('');
        wrap(<When value={now - 5 * 3_600_000} now={now} />);
        const time = screen.getByText('5 h ago');
        expect(time.tagName).toBe('TIME');
        expect(time.getAttribute('title')).toBeTruthy();
        expect(time.getAttribute('dateTime')).toBe(new Date(now - 5 * 3_600_000).toISOString());
    });
});

describe('one primary colour', () => {
    it('antd’s primary is the Cairn accent inside the workspace and in its dialogs and popups', () => {
        expect(PRIMARY_CSS).toContain(`body.cairn-ui .ant-btn-primary:not([disabled])`);
        expect(PRIMARY_CSS).toContain(tokens.color.primary);
        expect(PRIMARY_CSS).not.toMatch(/#1890ff/i);
    });

    it('the workspace marks the page while it is mounted', () => {
        const { client, cache } = makeClient({ 'GET /ui/config': UI_CONFIG });
        const { unmount } = render(<ConsumerApp client={client} cache={cache} />);
        expect(document.body.classList.contains('cairn-ui')).toBe(true);
        unmount();
        expect(document.body.classList.contains('cairn-ui')).toBe(false);
    });
});

describe('a quieter sidebar', () => {
    const ME = 'urn:li:corpuser:ana';
    const CAPS = { actor: ME, is_admin: true, privileges: { MANAGE_POLICIES: true, MANAGE_SYSTEM_OPERATIONS: true,
        MANAGE_GLOBAL_SETTINGS: true }, features: { onboarding: true } };
    const profileWith = (ui: unknown) => ({ urn: ME, role: 'Financial analyst', team: null, default_persona: null, language: null,
        pinned: [], instructions: null, notifications: { channels: { in_app: true }, events: {} }, personas: [],
        push: { enabled: false, subscriptions: 0 }, ui, updated_ms: null });
    const ui = (role: string | null, showAll = false) => ({ onboarding: { role, done: [], dismissed: false, asked: true },
        sidebar: { pinned: [], hidden: [], show_all: showAll } });

    function layoutAt(at: string, routes: Record<string, unknown>) {
        const { client, cache } = makeClient({ 'GET /ui/config': UI_CONFIG, 'GET /notify/inbox': [], 'GET /v1/me/capabilities': CAPS,
            ...routes });
        render(
            <MemoryRouter initialEntries={[at]}>
                <I18nProvider initial="en">
                    <CairnProvider client={client} cache={cache}><ConsumerLayout /></CairnProvider>
                </I18nProvider>
            </MemoryRouter>,
        );
        return screen.getByRole('navigation', { name: 'Workspace sections' });
    }
    const expanded = (nav: HTMLElement, name: string) => within(nav).getByRole('button', { name }).getAttribute('aria-expanded');

    afterEach(() => window.localStorage.clear());

    it('opens the groups of the person’s role and of the current page; the rest are one click away', async () => {
        const nav = layoutAt('/flows', { 'GET /v1/me/profile': profileWith(ui('analyst')) });
        await waitFor(() => expect(expanded(nav, 'Analyze')).toBe('true'));
        expect(expanded(nav, 'Catalog')).toBe('true');
        expect(expanded(nav, 'Build')).toBe('true');                  // the current page (Flows) is in Build
        expect(expanded(nav, 'Govern')).toBe('false');
        expect(within(nav).queryByRole('link', { name: 'Access policies' })).toBeNull();
        fireEvent.click(within(nav).getByRole('button', { name: 'Govern' }));
        expect(within(nav).getByRole('link', { name: 'Access policies' })).toBeTruthy();
        // Remembered for this person on this device.
        expect(JSON.parse(window.localStorage.getItem('cairn.sidebar.groups') || '{}')[ME]).toEqual({ 'nav.group.govern': true });
    });

    it('"Show everything" opens every group', async () => {
        let stored = profileWith(ui('analyst'));
        const nav = layoutAt('/discover', {
            'GET /v1/me/profile': () => stored,
            'PUT /v1/me/profile': (b: unknown) => {
                stored = profileWith(ui('analyst', !!(b as { ui: { sidebar: { show_all: boolean } } }).ui.sidebar.show_all));
                return stored;
            },
        });
        const everything = await screen.findByRole('button', { name: /^Show everything/ });
        expect(expanded(nav, 'Platform')).toBe('false');
        fireEvent.click(everything);
        await waitFor(() => expect(expanded(nav, 'Platform')).toBe('true'));
        for (const b of within(nav).getAllByRole('button')) expect(b.getAttribute('aria-expanded')).toBe('true');
        for (const name of ['Access policies', 'Ingestion', 'Settings', 'Platform map']) {
            expect(within(nav).getByRole('link', { name })).toBeTruthy();
        }
    });
});
