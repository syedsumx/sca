import React from 'react';
import { useLocation } from 'react-router-dom';

import { AssistantChat } from '../components/Assistant';
import { Page, Surface } from '../components/tokens';
import { useT } from '../i18n';
import { PageHeader } from '../guide/PageHeader';

export function AssistantPage() {
    const t = useT();
    const q = new URLSearchParams(useLocation().search).get('q') ?? undefined;
    return (
        <Page>
            <PageHeader title={t('page.assistant')} description={t('page.assistantIntro')} />
            <Surface style={{ padding: 16, marginTop: 16 }}>
                <AssistantChat initialQuestion={q} />
            </Surface>
        </Page>
    );
}
