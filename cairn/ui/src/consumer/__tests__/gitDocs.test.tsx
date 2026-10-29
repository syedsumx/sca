import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { GitRepositories, SendDocsToDbt } from '../components/GitDocs';
import { CairnProvider } from '../context/CairnProvider';
import { DS, MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

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

it('sends docs to the dbt repository that builds the dataset', async () => {
    const calls: MockCall[] = [];
    wrap(
        <SendDocsToDbt urn={DS} />,
        {
            'GET /integrations/git/match': { repo: 'analytics', model: 'orders' },
            'POST /integrations/git/writeback': {
                repo: 'analytics',
                model: 'orders',
                file: 'models/schema.yml',
                pull_request: 'https://github.com/acme/dbt/pull/8',
            },
        },
        calls,
    );
    fireEvent.click(await screen.findByRole('button', { name: 'Send docs to dbt' }));
    await waitFor(() => expect(calls.find((c) => c.method === 'POST')?.body).toEqual({ urn: DS }));
    expect(await screen.findByText('https://github.com/acme/dbt/pull/8')).toBeTruthy();
});

it('lists watched repositories and what they lack', async () => {
    wrap(<GitRepositories />, {
        'GET /integrations/git/repos': [
            {
                id: 'analytics',
                provider: 'github',
                repo: 'acme/analytics-dbt',
                base_branch: 'main',
                dbt_project_dir: '',
                platform: 'snowflake',
                database: 'analytics',
                schema: 'core',
                token_set: true,
                webhook_secret_set: false,
            },
        ],
    });
    expect(await screen.findByText('acme/analytics-dbt')).toBeTruthy();
    expect(screen.getByText('no webhook secret')).toBeTruthy();
});
