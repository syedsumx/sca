import React from 'react';
import { useLocation } from 'react-router-dom';

import { AssistantChat } from '../components/Assistant';
import { Muted, Page, SectionTitle, Surface } from '../components/tokens';
import { useT } from '../i18n';

export function AssistantPage() {
    const t = useT();
    const q = new URLSearchParams(useLocation().search).get('q') ?? undefined;
    return (
        <Page>
            <SectionTitle as="h1" style={{ fontSize: 24 }}>
                {t('page.assistant')}
            </SectionTitle>
            <Muted>{t('page.assistantIntro')}</Muted>
            <Surface style={{ padding: 16, marginTop: 16 }}>
                <AssistantChat initialQuestion={q} />
            </Surface>
        </Page>
    );
}
