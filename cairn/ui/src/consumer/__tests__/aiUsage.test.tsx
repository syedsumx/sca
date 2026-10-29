import { render, screen } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { UsedByAi } from '../components/AiUsage';
import { CairnProvider } from '../context/CairnProvider';
import { DS, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

it('shows which agents and AI tools read a dataset, with their AI system', async () => {
    const { client } = makeClient({
        'GET /ui/config': UI_CONFIG,
        'GET /ai/lineage/readers': {
            urn: DS,
            days: 30,
            readers: [
                {
                    agent: 'agt_1',
                    actor: null,
                    channel: 'api',
                    people: 2,
                    data_reads: 7,
                    metadata_reads: 1,
                    days: 3,
                    last_ms: 1,
                    agent_name: 'Revenue bot',
                    agent_owner: 'urn:li:corpuser:ana',
                    ai_system: { id: 'ais_1', name: 'Revenue forecasting', state: 'APPROVED', tier: 'limited' },
                },
                {
                    agent: null,
                    actor: null,
                    channel: 'assistant',
                    people: 1,
                    data_reads: 0,
                    metadata_reads: 4,
                    days: 1,
                    last_ms: 1,
                    agent_name: null,
                    agent_owner: null,
                    ai_system: null,
                },
            ],
        },
    });
    render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                <UsedByAi urn={DS} />
            </CairnProvider>
        </MemoryRouter>,
    );
    expect(await screen.findByText('Revenue bot')).toBeTruthy();
    expect(screen.getByText('Revenue forecasting')).toBeTruthy();
    expect(screen.getByText('approved')).toBeTruthy();
    expect(screen.getByText('Assistant')).toBeTruthy();
    expect(screen.getByText('not registered')).toBeTruthy();
    expect(screen.getByText('A person')).toBeTruthy();
});
