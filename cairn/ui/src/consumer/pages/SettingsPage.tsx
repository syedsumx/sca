import { Alert, Button, Checkbox, Input, InputNumber, List, Switch, Tabs, Typography, message } from 'antd';
import React, { useEffect, useState } from 'react';

import { AgentFleet } from '../../agents/AgentFleet';
import { CairnClientProvider } from '../../api/hooks';
import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { EmbedScope, EmbedToken, InboxItem, NotifySettings } from '../api/types';
import { AiAgentsSettings } from '../components/AiAgents';
import { ExtensionsAdmin } from '../components/Extensions';
import { LicenceSettings } from '../components/Licence';
import { Muted, Page, SectionTitle, Surface } from '../components/tokens';
import { useDheSession } from '../context/CairnProvider';
import { relativeTime } from '../utils/format';

export const EVENT_COPY: Record<string, string> = {
    'access.requested': 'Someone requests access to data I own',
    'access.granted': 'My access request is approved',
    'access.rejected': 'My access request is declined',
    'access.expiring': 'My access is about to expire',
    'certification.requested': 'Certification needs my review',
    'certification.decided': 'A certification I requested is decided',
    'asset.deprecated': 'An asset I follow is deprecated',
    'quality.incident.raised': 'A data quality incident on something I follow',
    'quality.incident.resolved': 'A data quality incident is resolved',
    'discussion.mention': 'I am @mentioned',
    'discussion.reply': 'Replies to my discussions',
    'context.drift': 'Definition drift on assets I own',
    'form.assigned': 'A compliance form needs my answers',
    'form.due': 'My compliance form tasks are overdue',
    'contract.broken': 'A data contract on something I own, follow or subscribe to breaks',
    'contract.restored': 'A broken data contract holds again',
    'automation.applied': 'An automation that notifies me changed assets',
    'asset.deprecation_proposed': 'Someone proposes deprecating an asset I own or follow',
};

function NotificationSettings() {
    const { client, cache } = useConsumerApi();
    const session = useDheSession();
    const settings = useDheQuery<NotifySettings>('/notify/settings', (c, signal) => c.getNotifySettings({ signal }));
    const [draft, setDraft] = useState<NotifySettings | null>(null);
    const [busy, setBusy] = useState(false);
    useEffect(() => {
        if (settings.data) setDraft(settings.data);
    }, [settings.data]);
    if (settings.error) return <Alert type="error" message={settings.error.message} />;
    if (!draft) return <Muted>Loading…</Muted>;
    const ch = draft.channels;
    const setCh = (patch: Partial<NotifySettings['channels']>) => setDraft({ ...draft, channels: { ...ch, ...patch } });
    const save = async () => {
        setBusy(true);
        try {
            cache.set('/notify/settings', await client.saveNotifySettings(draft));
            message.success('Notification preferences saved');
        } catch (e) {
            message.error((e as Error).message);
        } finally {
            setBusy(false);
        }
    };
    return (
        <Surface style={{ padding: 18 }}>
            <SectionTitle>Channels</SectionTitle>
            <p>
                <Switch checked={ch.slack.enabled} disabled={!session.config?.slack_enabled} onChange={(v) => setCh({ slack: { ...ch.slack, enabled: v } })} aria-label="Slack" />{' '}
                Slack direct messages {session.config && !session.config.slack_enabled && <Muted>(Slack not configured by your admin)</Muted>}
            </p>
            {ch.slack.enabled && (
                <Input
                    aria-label="Slack member ID"
                    style={{ maxWidth: 320, marginBottom: 12 }}
                    placeholder="Slack member ID (optional — matched by email)"
                    value={ch.slack.user_id ?? ''}
                    onChange={(e) => setCh({ slack: { ...ch.slack, user_id: e.target.value || null } })}
                />
            )}
            <p>
                <Switch checked={ch.teams.enabled} disabled={!session.config?.teams_enabled} onChange={(v) => setCh({ teams: { ...ch.teams, enabled: v } })} aria-label="Microsoft Teams" />{' '}
                Microsoft Teams
            </p>
            {ch.teams.enabled && (
                <Input
                    aria-label="Teams workflow webhook URL"
                    style={{ maxWidth: 520, marginBottom: 12 }}
                    placeholder="Personal Teams Workflows webhook URL"
                    value={ch.teams.webhook_url ?? ''}
                    onChange={(e) => setCh({ teams: { ...ch.teams, webhook_url: e.target.value || null } })}
                />
            )}
            <p>
                <Switch checked={ch.email.enabled} onChange={(v) => setCh({ email: { enabled: v } })} aria-label="Email" /> Email digest
            </p>
            <p>
                <Switch checked={ch.in_app.enabled} onChange={(v) => setCh({ in_app: { enabled: v } })} aria-label="In-app" /> In-app inbox
            </p>
            <SectionTitle>Notify me when…</SectionTitle>
            {Object.entries(EVENT_COPY).map(([evt, copy]) => (
                <div key={evt}>
                    <Checkbox
                        checked={draft.events[evt] ?? true}
                        onChange={(e) => setDraft({ ...draft, events: { ...draft.events, [evt]: e.target.checked } })}
                    >
                        {copy}
                    </Checkbox>
                </div>
            ))}
            <Button type="primary" style={{ marginTop: 16 }} loading={busy} onClick={() => void save()}>
                Save preferences
            </Button>
        </Surface>
    );
}

function Inbox() {
    const { client, cache } = useConsumerApi();
    const inbox = useDheQuery<InboxItem[]>('/notify/inbox', (c, signal) => c.inbox(false, { signal }));
    const unread = (inbox.data ?? []).filter((i) => !i.read_at).map((i) => i.id);
    return (
        <Surface style={{ padding: 12 }}>
            <Button
                disabled={!unread.length}
                onClick={() =>
                    client.markRead(unread).then(
                        () => cache.invalidate('/notify/inbox'),
                        (e: Error) => message.error(e.message),
                    )
                }
            >
                Mark all read
            </Button>
            <List
                loading={inbox.loading}
                dataSource={inbox.data ?? []}
                locale={{ emptyText: 'No notifications' }}
                renderItem={(i) => (
                    <List.Item
                        extra={<Muted>{relativeTime(i.created_at)}</Muted>}
                        actions={i.action_url ? [<a key="open" href={i.action_url}>Open</a>] : undefined}
                        style={{ fontWeight: i.read_at ? 400 : 600 }}
                    >
                        <List.Item.Meta title={i.title} description={i.body} />
                    </List.Item>
                )}
            />
        </Surface>
    );
}

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
            message.error((e as Error).message);
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

/** Pools, enrolment, agents and their runs; for people who manage ingestion. */
export function RemoteAgentsSettings() {
    const { client } = useConsumerApi();
    const session = useDheSession();
    return (
        <CairnClientProvider client={client}>
            <AgentFleet embedded gatewayUrl={session.config?.gateway_url} />
        </CairnClientProvider>
    );
}

export function SettingsPage() {
    const session = useDheSession();
    return (
        <Page>
            <SectionTitle as="h1" style={{ fontSize: 24 }}>
                Settings
            </SectionTitle>
            <Tabs
                items={[
                    { key: 'notifications', label: 'Notifications', children: <NotificationSettings /> },
                    { key: 'inbox', label: 'Inbox', children: <Inbox /> },
                    { key: 'embeds', label: 'Embeds', children: <EmbedBuilder /> },
                    {
                        key: 'ai-agents',
                        label: 'AI agents',
                        children: <AiAgentsSettings isAdmin={!!session.config?.is_admin} />,
                    },
                    ...(session.config?.can_manage_agents
                        ? [{ key: 'agents', label: 'Remote agents', children: <RemoteAgentsSettings /> }]
                        : []),
                    ...(session.config?.is_admin
                        ? [
                              { key: 'extensions', label: 'Extensions', children: <ExtensionsAdmin /> },
                              { key: 'licence', label: 'Licence', children: <LicenceSettings /> },
                          ]
                        : []),
                ]}
            />
        </Page>
    );
}
