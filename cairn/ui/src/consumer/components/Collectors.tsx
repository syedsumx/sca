import React from 'react';

import { AgentFleet } from '../../agents/AgentFleet';
import { CairnClientProvider } from '../../api/hooks';
import { useConsumerApi } from '../api/hooks';
import { useDheSession } from '../context/CairnProvider';
import { Muted } from './tokens';

/**
 * Ingestion › Collectors (formerly "remote agents"): pools, enrolment, the collectors that run metadata ingestion
 * inside other networks, and their runs. The API keeps its `/agents` paths; `/collectors` answers the same.
 */
export function CollectorsView() {
    const { client } = useConsumerApi();
    const session = useDheSession();
    return (
        <>
            <Muted style={{ display: 'block', marginBottom: 12 }}>
                A collector runs metadata ingestion inside a network Cairn cannot reach and reports back; it is not an AI
                agent (those are in AI › Agents). Start one with <code>cairn collectors run</code>.
            </Muted>
            <CairnClientProvider client={client}>
                <AgentFleet embedded gatewayUrl={session.config?.gateway_url} />
            </CairnClientProvider>
        </>
    );
}
