import { Alert, Button, Drawer, Space, Spin } from 'antd';
import React, { useEffect, useState } from 'react';
import { useHistory, useLocation } from 'react-router-dom';

import { Muted } from '../components/tokens';
import { useT } from '../i18n';
import { RESOURCE_SPECS } from '../resources/catalog';
import { HUBS } from '../resources/hubs';
import { MarkdownView } from '../utils/markdown';
import { useGuide } from './GuideProvider';
import { HelpSnippet, docsHref, helpAnchorFor, helpFor, pageTitleFor } from './help';

/** The docs' section about the page on screen, beside it: bundled with the app, so it opens offline too. */
export function HelpPanel({ pageTitle }: { pageTitle: (pathname: string) => string | undefined }) {
    const g = useGuide();
    const t = useT();
    const location = useLocation();
    const history = useHistory();
    const anchor = helpAnchorFor(location.pathname, location.search, HUBS, RESOURCE_SPECS);
    const [snippet, setSnippet] = useState<HelpSnippet | null | undefined>(undefined);
    useEffect(() => {
        if (!g.help) return undefined;
        let live = true;
        setSnippet(undefined);
        helpFor(anchor).then((s) => live && setSnippet(s), () => live && setSnippet(null));
        return () => {
            live = false;
        };
    }, [g.help, anchor]);
    const raw = pageTitleFor(location.pathname, location.search, HUBS, RESOURCE_SPECS);
    const page = pageTitle(location.pathname) ?? raw;
    const section = snippet?.title ?? page;
    const ask = () => {
        g.setHelp(false);
        history.push(`/assistant?q=${encodeURIComponent(t('help.askPrompt', { page, section }))}`);
    };
    return (
        <Drawer
            open={g.help}
            onClose={() => g.setHelp(false)}
            placement="right"
            width={480}
            title={t('help.title', { page })}
            footer={
                <Space wrap>
                    <Button href={docsHref(anchor)} target="_blank" rel="noreferrer">{t('help.openDocs')}</Button>
                    <Button type="primary" onClick={ask}>{t('help.ask')}</Button>
                </Space>
            }
        >
            {g.help && (
                <div aria-live="polite">
                    {snippet === undefined ? (
                        <div aria-busy="true"><Spin /> <Muted>{t('help.loading')}</Muted></div>
                    ) : snippet === null ? (
                        <Alert type="info" showIcon message={t('help.none')} />
                    ) : (
                        <article aria-label={snippet.title}>
                            <h2 style={{ fontSize: 18, marginTop: 0 }}>{snippet.title}</h2>
                            <div className="cairn-help"><MarkdownView source={snippet.body} /></div>
                            {snippet.more && <p><Muted>{t('help.more')}</Muted></p>}
                        </article>
                    )}
                </div>
            )}
        </Drawer>
    );
}
