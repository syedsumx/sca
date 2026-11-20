import React from 'react';
import { useLocation } from 'react-router-dom';

import { AssistantChat } from '../components/Assistant';
import { Page, Surface } from '../components/tokens';
import { useT } from '../i18n';
import { takeHandedOff } from '../guide/askHandoff';
import { PageHeader } from '../guide/PageHeader';

export function AssistantPage() {
    const t = useT();
    const q = new URLSearchParams(useLocation().search).get('q') ?? undefined;
    // Sent at once only when Cairn itself handed the question over (palette, search bar, help; askHandoff). A bare
    // ?q= from anywhere else is only put in the box.
    const [sendNow] = React.useState(() => takeHandedOff(q));
    return (
        <Page>
            <PageHeader title={t('page.assistant')} description={t('page.assistantIntro')} />
            <Surface style={{ padding: 16, marginTop: 16 }}>
                <AssistantChat initialQuestion={q} sendNow={sendNow} />
            </Surface>
        </Page>
    );
}
