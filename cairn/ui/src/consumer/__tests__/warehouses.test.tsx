/**
 * L151, Snowflake and Databricks tables in place: Lakehouse › External catalogs leads with a card per warehouse preset,
 * each opening the area's generated form for that preset; the forms take the presets' fields; the strings are
 * translated (German complete).
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import React from 'react';
import { MemoryRouter, Route } from 'react-router-dom';

import { ExternalCatalogsView, WAREHOUSE_PRESETS } from '../components/WarehouseConnect';
import { CairnProvider } from '../context/CairnProvider';
import { I18nProvider, translate } from '../i18n';
import { WAREHOUSES_DE, WAREHOUSES_EN } from '../i18n/warehouses';
import { RESOURCE_SPECS } from '../resources/catalog';
import { FORMS } from '../resources/forms';
import { UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const SNOW = {
    name: 'snow', preset: 'snowflake_open_catalog', platform: 'snowflake', connector: 'iceberg',
    uri: 'https://o-a.snowflakecomputing.com/polaris/api/catalog', auth: 'oauth2', writable: false,
    trino_state: 'attached', last_sync_ms: Date.now() - 60_000,
};

function wrap(at = '/lakehouse?tab=external-catalogs', locale: 'en' | 'de' = 'en') {
    const { client, cache } = makeClient({ 'GET /ui/config': UI_CONFIG, 'GET /v1/external-catalogs': { catalogs: [SNOW] } });
    let where = '';
    const out = render(
        <MemoryRouter initialEntries={[at]}>
            <I18nProvider initial={locale}>
                <CairnProvider client={client} cache={cache}>
                    <ExternalCatalogsView />
                    <Route render={({ location }) => { where = `${location.pathname}${location.search}`; return null; }} />
                </CairnProvider>
            </I18nProvider>
        </MemoryRouter>,
    );
    return { ...out, where: () => where };
}

describe('warehouse presets', () => {
    it('shows a card per preset with how the data is reached, and the registered catalogs below', async () => {
        wrap();
        const cards = screen.getByRole('list', { name: 'Connect a warehouse' });
        expect(within(cards).getAllByRole('listitem')).toHaveLength(5);
        for (const name of ['Snowflake Open Catalog', 'Snowflake-managed Iceberg', 'Databricks Unity Catalog',
            'Snowflake, live', 'Databricks Delta Sharing']) {
            expect(within(cards).getByRole('region', { name })).toBeTruthy();
        }
        expect(within(cards).getAllByText('In place')).toHaveLength(3);
        expect(within(cards).getByText('Live queries')).toBeTruthy();
        expect(screen.getByText(/Snowflake bills them/)).toBeTruthy();       // live queries use Snowflake's compute
        await waitFor(() => expect(screen.getByText('snowflake_open_catalog')).toBeTruthy());
        expect(screen.getByText('snowflake')).toBeTruthy();                   // the platform its tables are named under
    });

    it('a card opens the generated form for its preset, filled with the preset', async () => {
        const v = wrap();
        fireEvent.click(screen.getByRole('link', { name: 'Connect Databricks Unity Catalog' }));
        const dialog = await screen.findByRole('dialog');
        expect(within(dialog).getAllByText(/Connect Databricks Unity Catalog/).length).toBeGreaterThan(0);
        expect(dialog.querySelector('.ant-select-selection-item[title="databricks_unity"]')).toBeTruthy();   // the preset, chosen
        expect(v.where()).toBe('/lakehouse?tab=external-catalogs');          // the action is opened once, then dropped
        const link = screen.getByRole('link', { name: 'Connect Databricks Delta Sharing' });
        expect(link.getAttribute('href')).toBe(`/ingestion?tab=saas-syncs&action=${encodeURIComponent('Read a Delta Sharing table')}`);
    });

    it('every card names an action the area has, and the forms take the presets’ fields', () => {
        for (const p of WAREHOUSE_PRESETS) {
            const spec = RESOURCE_SPECS[p.tab];
            expect(spec.actions?.some((a) => a.label === p.action && !/\{\w+\}/.test(a.path))).toBe(true);
        }
        const ext = FORMS['POST /v1/external-catalogs'].fields;
        const preset = ext.find((f) => f.name === 'preset');
        expect(preset?.type).toBe('select');
        expect(preset?.enum).toEqual(['iceberg', 'snowflake_open_catalog', 'snowflake_horizon', 'databricks_unity', 'snowflake_federated']);
        expect(ext.map((f) => f.name)).toEqual(expect.arrayContaining(['database', 'role', 'owner', 'domain', 'credential_ref']));
        expect(ext.find((f) => f.name === 'credential_ref')?.picker).toBe('secret_ref');   // a reference, never a value
        const saas = FORMS['POST /v1/saas-syncs'].fields.find((f) => f.name === 'kind');
        expect(saas?.enum).toContain('delta_sharing');
    });

    it('is translated: German complete, the others with the titles', () => {
        expect(Object.keys(WAREHOUSES_DE).sort()).toEqual(Object.keys(WAREHOUSES_EN).sort());
        expect(translate('de', 'wh.title')).toBe('Ein Data Warehouse verbinden');
        expect(translate('fr', 'wh.connect', { name: 'Snowflake' })).toBe('Connecter Snowflake');
        expect(translate('ja', 'wh.intro')).toBe(WAREHOUSES_EN['wh.intro']);    // falls back to English
        wrap('/lakehouse?tab=external-catalogs', 'de');
        expect(screen.getByRole('heading', { name: 'Ein Data Warehouse verbinden' })).toBeTruthy();
    });
});
