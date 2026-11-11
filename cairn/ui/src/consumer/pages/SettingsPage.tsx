import { Alert, Button, Checkbox, Input, InputNumber, List, Switch, Tabs, Typography, message } from 'antd';
import React, { useEffect, useState } from 'react';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { EmbedScope, EmbedToken, InboxItem, NotifySettings } from '../api/types';
import { ExtensionsAdmin } from '../components/Extensions';
import { LicenceSettings } from '../components/Licence';
import { Muted, Page, SectionTitle, Surface } from '../components/tokens';
import { useDheSession } from '../context/CairnProvider';
import { IacPanel } from '../resources/IacPanel';
import { relativeTime } from '../utils/format';
import { useT } from '../i18n';
import { GuideSettings } from '../guide/GuideSettings';
import { ErrorNotice, notifyError } from '../components/ErrorNotice';
import { PageHeader } from '../guide/PageHeader';
import { pageHelp } from '../guide/help';

export function EmbedBuilder() {
    const { client } = useConsumerApi();
    const session = useDheSession();
    const [urn, setUrn] = useState('');
    const [scopes, setScopes] = useState<EmbedScope[]>(['card', 'quality']);
    const [ttlDays, setTtlDays] = useState(30);
    const [origin, setOrigin] = useState('');
    const [token, setToken] = useState<EmbedToken | null>(null);
    const [busy, setBusy] = useState(false);
    const create = async () => {
        setBusy(true);
        try {
            setToken(
                await client.createEmbedToken({
                    urn: urn.trim(),
                    scopes,
                    ttl_seconds: ttlDays * 86400,
                    audience_origin: origin.trim() || undefined,
                }),
            );
        } catch (e) {
            notifyError(e);
        } finally {
            setBusy(false);
        }
    };
    return (
        <Surface style={{ padding: 18 }}>
            <p>Embed trust badges, asset cards and lineage into Looker, Tableau, Power BI or internal portals.</p>
            <Input aria-label="Asset URN" placeholder="urn:li:dataset:(…) or urn:li:dashboard:(…)" value={urn} onChange={(e) => setUrn(e.target.value)} />
            <div style={{ margin: '12px 0' }}>
                <Checkbox.Group
                    aria-label="Embed scopes"
                    value={scopes}
                    onChange={(v) => setScopes(v as EmbedScope[])}
                    options={[
                        { label: 'Asset card', value: 'card' },
                        { label: 'Quality badge', value: 'quality' },
                        { label: 'Lineage', value: 'lineage' },
                    ]}
                />
            </div>
            <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
                <span>
                    Valid for <InputNumber aria-label="Days valid" min={1} max={30} value={ttlDays} onChange={(v) => setTtlDays(Number(v) || 1)} /> days
                </span>
                <Input
                    aria-label="Embedding origin"
                    style={{ maxWidth: 320 }}
                    placeholder="https://yourcompany.looker.com (optional)"
                    value={origin}
                    onChange={(e) => setOrigin(e.target.value)}
                />
                <Button type="primary" disabled={!urn.trim() || !scopes.length} loading={busy} onClick={() => void create()}>
                    Create embed
                </Button>
            </div>
            {session.config?.embed_allowed_origins?.length ? (
                <Muted style={{ display: 'block', marginTop: 8, fontSize: 12 }}>
                    Allowed embedding origins: {session.config.embed_allowed_origins.join(', ')}
                </Muted>
            ) : null}
            {token && (
                <div style={{ marginTop: 16 }}>
                    <Alert type="info" showIcon message={`Expires ${new Date(token.expires_at).toLocaleString()}. Anyone with this snippet can see the embedded summary.`} />
                    <SectionTitle>Web component</SectionTitle>
                    <Typography.Paragraph copyable={{ text: token.snippet_html }}>
                        <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12 }}>{token.snippet_html}</pre>
                    </Typography.Paragraph>
                    <SectionTitle>iframe URL (Tableau extension, Power BI web content, Looker)</SectionTitle>
                    <Typography.Paragraph copyable={{ text: token.iframe_url }}>
                        <code>{token.iframe_url}</code>
                    </Typography.Paragraph>
                </div>
            )}
        </Surface>
    );
}

export function SettingsPage() {
    const session = useDheSession();
    const t = useT();
    // Links into a tab (a notification about a change request: ?tab=iac&change=<id>); read without a router, since
    // the page is also mounted on its own.
    const params = new URLSearchParams(typeof window !== 'undefined' ? window.location.search : '');
    return (
        <Page>
            <PageHeader title={t('settings.title')} help={pageHelp('/settings')}
                description="The platform's own settings: the guide, infrastructure as code and its change requests, embeds, extensions and the licence." />
            <Tabs
                defaultActiveKey={params.get('tab') ?? undefined}
                items={[
                    ...(session.config?.is_admin
                        ? [{ key: 'guide', label: t('settings.guide'), children: <GuideSettings /> }]
                        : []),
                    { key: 'iac', label: t('settings.iac'), children: <IacPanel change={params.get('change')} /> },
                    { key: 'embeds', label: t('settings.embeds'), children: <EmbedBuilder /> },
                    ...(session.config?.is_admin
                        ? [
                              { key: 'extensions', label: t('settings.extensions'), children: <ExtensionsAdmin /> },
                              { key: 'licence', label: t('settings.licence'), children: <LicenceSettings /> },
                          ]
                        : []),
                ]}
            />
        </Page>
    );
}
