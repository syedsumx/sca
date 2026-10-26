import React from 'react';
import { useLocation } from 'react-router-dom';

import { AssistantChat } from '../components/Assistant';
import { Muted, Page, SectionTitle, Surface } from '../components/tokens';

export function AssistantPage() {
    const q = new URLSearchParams(useLocation().search).get('q') ?? undefined;
    return (
        <Page>
            <SectionTitle as="h1" style={{ fontSize: 24 }}>
                ✨ Assistant
            </SectionTitle>
            <Muted>Converse with your metadata: find data, understand metrics, explain lineage and get docs drafted.</Muted>
            <Surface style={{ padding: 16, marginTop: 16 }}>
                <AssistantChat initialQuestion={q} />
            </Surface>
        </Page>
    );
}
