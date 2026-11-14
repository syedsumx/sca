import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { ProductSlos, SloDashboard } from '../components/Slos';
import { CairnProvider } from '../context/CairnProvider';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const SLO = {
    id: 'orders-freshness',
    product_urn: 'urn:li:dataProduct:orders',
    name: 'Orders land by 06:00',
    kind: 'freshness',
    objective: 99,
    window_days: 28,
    description: '',
    datasets: [],
    monitors: 2,
    good: 95,
    total: 100,
    sli: 95,
    budget_remaining: 0,
    burn_rate: 3.5,
    state: 'breached',
};

function wrap(node: React.ReactNode, routes: Record<string, unknown>, calls: MockCall[] = []) {
    const { client } = makeClient({ 'GET /ui/config': UI_CONFIG, ...routes }, calls);
    render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                {node}
            </CairnProvider>
        </MemoryRouter>,
    );
}

it('shows SLOs worst first with their budget', async () => {
    wrap(<SloDashboard />, { 'GET /marketplace/slos': [SLO] });
    expect(await screen.findByText('Orders land by 06:00')).toBeTruthy();
    expect(screen.getByText('1 SLO is at risk or breached')).toBeTruthy();
    expect(screen.getByText('3.5×')).toBeTruthy();
});

it('lets an owner add an SLO to a product', async () => {
    const calls: MockCall[] = [];
    wrap(
        <ProductSlos urn="urn:li:dataProduct:orders" canEdit />,
        {
            'GET /marketplace/slos': [],
            'PUT /marketplace/slos/orders-freshness': SLO,
        },
        calls,
    );
    fireEvent.change(await screen.findByLabelText('SLO name'), { target: { value: 'Orders land by 06:00' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add SLO' }));
    await waitFor(() =>
        expect(calls.find((c) => c.method === 'PUT')?.body).toMatchObject({
            id: 'orders-freshness',
            kind: 'freshness',
            objective: 99,
            window_days: 28,
            product_urn: 'urn:li:dataProduct:orders',
        }),
    );
});
