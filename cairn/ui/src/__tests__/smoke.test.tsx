import React from 'react';
import { render, waitFor } from '@testing-library/react';

import { CairnClient } from '../api/client';
import { CairnClientProvider } from '../api/hooks';
import { ENTERPRISE_ROUTES } from '../routes';
import { DatasetHealthBadge } from '../quality/DatasetHealthBadge';

beforeAll(() => {
    // antd responsive components need matchMedia in jsdom.
    Object.defineProperty(window, 'matchMedia', {
        writable: true,
        value: (query: string) => ({
            matches: false,
            media: query,
            onchange: null,
            addListener: () => undefined,
            removeListener: () => undefined,
            addEventListener: () => undefined,
            removeEventListener: () => undefined,
            dispatchEvent: () => false,
        }),
    });
});

function emptyApi(): typeof fetch {
    return (async (url: string) => {
        const path = String(url);
        let body: unknown = [];
        if (path.endsWith('/governance/policies')) body = { policies: [] };
        if (path.includes('/health')) body = { dataset_urn: 'u', status: 'FAIL', monitors: [], open_incidents: 2 };
        return new Response(JSON.stringify(body), { status: 200 });
    }) as unknown as typeof fetch;
}

describe('page smoke tests', () => {
    ENTERPRISE_ROUTES.forEach((route) => {
        it(`renders ${route.path} without crashing`, async () => {
            const client = new CairnClient({ fetchImpl: emptyApi() });
            const Page = route.component;
            const { container } = render(
                <CairnClientProvider client={client}>
                    <Page />
                </CairnClientProvider>,
            );
            await waitFor(() => expect(container.querySelector('.ant-spin-spinning')).toBeNull());
            expect(container.textContent?.length).toBeGreaterThan(0);
        });
    });

    it('DatasetHealthBadge shows failing status from the API', async () => {
        const client = new CairnClient({ fetchImpl: emptyApi() });
        const { findByText } = render(
            <CairnClientProvider client={client}>
                <DatasetHealthBadge datasetUrn="urn:li:dataset:(urn:li:dataPlatform:x,y,PROD)" />
            </CairnClientProvider>,
        );
        expect(await findByText('Failing')).toBeTruthy();
    });
});
