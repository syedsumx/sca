import { Button, Checkbox, Input, List, Switch, message } from 'antd';
import React, { useEffect, useState } from 'react';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { InboxItem, NotifySettings } from '../api/types';
import { useDheSession } from '../context/CairnProvider';
import { relativeTime } from '../utils/format';
import { ErrorNotice, notifyError } from './ErrorNotice';
import { Muted, SectionTitle, Surface } from './tokens';

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

/** Profile › Notifications: each channel (Slack member ID, Teams webhook, email, in-app) and the events that notify you. */
export function NotificationSettings() {
    const { client, cache } = useConsumerApi();
    const session = useDheSession();
    const settings = useDheQuery<NotifySettings>('/notify/settings', (c, signal) => c.getNotifySettings({ signal }));
    const [draft, setDraft] = useState<NotifySettings | null>(null);
    const [busy, setBusy] = useState(false);
    useEffect(() => {
        if (settings.data) setDraft(settings.data);
    }, [settings.data]);
    if (settings.error) return <ErrorNotice error={settings.error} />;
    if (!draft) return <Muted>Loading…</Muted>;
    const ch = draft.channels;
    const setCh = (patch: Partial<NotifySettings['channels']>) => setDraft({ ...draft, channels: { ...ch, ...patch } });
    const save = async () => {
        setBusy(true);
        try {
            cache.set('/notify/settings', await client.saveNotifySettings(draft));
            message.success('Notification preferences saved');
        } catch (e) {
            notifyError(e);
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

/** The inbox's notifications, newest first (the header's Inbox). */
export function InboxList() {
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
                        (e: unknown) => notifyError(e),
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
