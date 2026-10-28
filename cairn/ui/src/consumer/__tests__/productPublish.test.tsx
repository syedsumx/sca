import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { ProductPublish } from '../components/ProductPublish';
import { CairnProvider } from '../context/CairnProvider';
import { MockCall, PRODUCT, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const PLAN = {
    product: PRODUCT.urn,
    target: 'snowflake',
    tables: ['shop.public.orders'],
    skipped: ['urn:li:dataset:pg'],
    manifest: 'title: Customer 360\n',
    statements: ['CREATE SHARE IF NOT EXISTS "C360"', 'GRANT SELECT ON TABLE "SHOP"."PUBLIC"."ORDERS" TO SHARE "C360"'],
    fingerprint: 'abc',
    published: null,
    in_sync: false,
};

it('plans, then records a publication done by hand', async () => {
    const calls: MockCall[] = [];
    const url = `/marketplace/products/${PRODUCT.urn}/publish/snowflake`;
    const { client } = makeClient(
        {
            'GET /ui/config': UI_CONFIG,
            [`POST ${url}/plan`]: PLAN,
            [`POST ${url}`]: {
                ...PLAN,
                applied: true,
                in_sync: true,
                published: { fingerprint: 'abc', published_by: 'x', published_ms: 1 },
            },
        },
        calls,
    );
    render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                <ProductPublish urn={PRODUCT.urn} />
            </CairnProvider>
        </MemoryRouter>,
    );
    fireEvent.change(screen.getByLabelText('Share name'), { target: { value: 'c360' } });
    fireEvent.change(screen.getByLabelText('Consumers'), { target: { value: 'ACME.PROD, GLOBEX.PROD' } });
    fireEvent.click(screen.getByRole('button', { name: 'Plan' }));
    expect((await screen.findByLabelText('Statements')).textContent).toContain('GRANT SELECT ON TABLE');
    expect(screen.getByText('not published yet')).toBeTruthy();
    expect(screen.getByText(/1 on other platforms skipped/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Record as published' }));
    expect(await screen.findByText('published, up to date')).toBeTruthy();
    await waitFor(() =>
        expect(calls.find((c) => c.path === url)?.body).toEqual({
            options: { share: 'c360', consumers: ['ACME.PROD', 'GLOBEX.PROD'] },
            connection: null,
        }),
    );
});
