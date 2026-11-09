import { Alert, Button, Checkbox, Empty, Input, Select, Space, Tag, message } from 'antd';
import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import styled from 'styled-components';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { PinKind, Profile, ProfileChannel, ProfileLanguage, ProfilePin } from '../api/types';
import { SignedInDevices } from '../components/SignedInDevices';
import { Muted, Page, SectionTitle, Surface, tokens } from '../components/tokens';
import { LOCALES, Locale, useI18n } from '../i18n';
import { deviceSubscribed, disablePush, enablePush, pushSupported } from '../utils/push';
import { ErrorNotice, describeError } from '../components/ErrorNotice';
import { PageHeader } from '../guide/PageHeader';
import { pageHelp } from '../guide/help';

const KINDS: { value: PinKind; label: string }[] = [
    { value: 'asset', label: 'Asset' },
    { value: 'dashboard', label: 'Dashboard' },
    { value: 'metric', label: 'Metric' },
    { value: 'query', label: 'Saved query' },
    { value: 'workbook', label: 'Workbook' },
];
const CHANNELS: { key: ProfileChannel; label: string; help?: string }[] = [
    { key: 'in_app', label: 'Inbox in Cairn', help: 'Always on' },
    { key: 'email', label: 'Email' },
    { key: 'slack', label: 'Slack direct messages' },
    { key: 'teams', label: 'Microsoft Teams' },
    { key: 'push', label: 'Push notifications on your devices' },
];
const MAX_INSTRUCTIONS = 2000;

const Grid = styled.div`
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: ${tokens.space(4)};
    @media (max-width: 720px) {
        grid-template-columns: minmax(0, 1fr);
    }
    label {
        display: block;
        font-weight: 600;
        margin-bottom: 4px;
    }
`;

const Section = styled(Surface)`
    padding: ${tokens.space(5)};
    margin-bottom: ${tokens.space(5)};
    @media (max-width: 600px) {
        padding: ${tokens.space(4)};
    }
`;

const Pins = styled.ul`
    list-style: none;
    margin: 0 0 ${tokens.space(3)};
    padding: 0;
    li {
        display: flex;
        align-items: center;
        gap: ${tokens.space(2)};
        padding: ${tokens.space(2)} 0;
        border-bottom: 1px solid ${tokens.color.border};
        min-width: 0;
    }
    li a,
    li span.label {
        flex: 1;
        min-width: 0;
        overflow-wrap: anywhere;
    }
`;

function pinLink(p: ProfilePin): string {
    switch (p.kind) {
        case 'asset':
            return `/asset/${encodeURIComponent(p.ref)}`;
        case 'dashboard':
            return '/dashboards';
        case 'metric':
            return '/sql/metrics';
        case 'workbook':
            return '/sql/workbooks';
        default:
            return '/sql';
    }
}

type Draft = Pick<Profile, 'role' | 'team' | 'default_persona' | 'language' | 'pinned' | 'instructions'> & {
    channels: Profile['notifications']['channels'];
};

function draftOf(p: Profile): Draft {
    return { role: p.role, team: p.team, default_persona: p.default_persona, language: p.language, pinned: p.pinned,
        instructions: p.instructions, channels: p.notifications.channels };
}

/** Your profile: who you are to Cairn, what you pinned, how you want to hear from it, and the assistant's standing
 * instructions. The assistant and your scheduled tasks read it as context; it never changes what you may see. */
export function ProfilePage() {
    const { client, cache } = useConsumerApi();
    const { setLocale } = useI18n();
    const profile = useDheQuery<Profile>('/v1/me/profile', (c, signal) => c.profile({ signal }));
    const [draft, setDraft] = useState<Draft | null>(null);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [pin, setPin] = useState<{ kind: PinKind; ref: string; label: string }>({ kind: 'asset', ref: '', label: '' });
    const [device, setDevice] = useState<boolean | null>(null);
    const [deviceBusy, setDeviceBusy] = useState(false);

    useEffect(() => {
        if (profile.data && !draft) setDraft(draftOf(profile.data));
    }, [profile.data, draft]);
    useEffect(() => {
        deviceSubscribed().then(setDevice, () => setDevice(false));
    }, []);

    if (profile.error) {
        return (
            <Page>
                <PageHeader title="Your profile" help={pageHelp('/profile')} />
                <ErrorNotice error={profile.error} />
            </Page>
        );
    }
    if (!profile.data || !draft) {
        return (
            <Page aria-busy="true">
                <PageHeader title="Your profile" help={pageHelp('/profile')} />
            </Page>
        );
    }
    const p = profile.data;
    const set = (patch: Partial<Draft>) => setDraft({ ...draft, ...patch });

    const save = async () => {
        setSaving(true);
        setError(null);
        try {
            const out = await client.saveProfile({
                role: draft.role ?? null, team: draft.team ?? null, default_persona: draft.default_persona ?? null,
                language: draft.language ?? null, pinned: draft.pinned, instructions: draft.instructions ?? null,
                notifications: { channels: draft.channels },
            });
            cache.set('/v1/me/profile', out);
            profile.mutate(out);
            setDraft(draftOf(out));
            if (out.language) setLocale(out.language as Locale);
            message.success('Profile saved');
        } catch (e) {
            setError(describeError(e));
        } finally {
            setSaving(false);
        }
    };

    const addPin = () => {
        const ref = pin.ref.trim();
        if (!ref) return;
        const next = { kind: pin.kind, ref, label: pin.label.trim() || ref };
        set({ pinned: [...draft.pinned.filter((x) => !(x.kind === next.kind && x.ref === next.ref)), next] });
        setPin({ ...pin, ref: '', label: '' });
    };

    const toggleDevice = async () => {
        setDeviceBusy(true);
        setError(null);
        try {
            if (device) {
                await disablePush(client);
                setDevice(false);
            } else {
                await enablePush(client, p.push.public_key ?? '');
                setDevice(true);
                if (!draft.channels.push) set({ channels: { ...draft.channels, push: true } });
            }
        } catch (e) {
            setError(describeError(e));
        } finally {
            setDeviceBusy(false);
        }
    };

    return (
        <Page>
            <PageHeader title="Your profile" help={pageHelp('/profile')}
                description="What Cairn knows about you to help: the assistant and your scheduled tasks use it as context. It never changes what you may see or do." />
            {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} />}

            <Section as="section" aria-labelledby="profile-about">
                <SectionTitle id="profile-about" style={{ marginTop: 0 }}>About you</SectionTitle>
                <Grid>
                    <div>
                        <label htmlFor="profile-role">Role</label>
                        <Input id="profile-role" maxLength={120} placeholder="e.g. Financial analyst" value={draft.role ?? ''}
                            onChange={(e) => set({ role: e.target.value })} />
                    </div>
                    <div>
                        <label htmlFor="profile-team">Team</label>
                        <Input id="profile-team" maxLength={120} placeholder="e.g. FP&A" value={draft.team ?? ''}
                            onChange={(e) => set({ team: e.target.value })} />
                    </div>
                    <div>
                        <label htmlFor="profile-persona">Default persona</label>
                        <Select id="profile-persona" aria-label="Default persona" style={{ width: '100%' }} allowClear
                            placeholder={p.personas.length ? 'Choose one of your personas' : 'You belong to no persona'}
                            disabled={!p.personas.length} value={draft.default_persona ?? undefined}
                            options={p.personas.map((x) => ({ value: x.id, label: x.name }))}
                            onChange={(v) => set({ default_persona: (v as string | undefined) ?? null })} />
                    </div>
                    <div>
                        <label htmlFor="profile-language">Language</label>
                        <Select id="profile-language" aria-label="Language" style={{ width: '100%' }} allowClear
                            placeholder="As the browser chooses" value={draft.language ?? undefined}
                            options={LOCALES.map((l) => ({ value: l.code, label: l.label }))}
                            onChange={(v) => set({ language: ((v as string | undefined) ?? null) as ProfileLanguage | null })} />
                    </div>
                </Grid>
            </Section>

            <Section as="section" aria-labelledby="profile-pins">
                <SectionTitle id="profile-pins" style={{ marginTop: 0 }}>Pinned</SectionTitle>
                {draft.pinned.length === 0 ? (
                    <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="Nothing pinned yet" />
                ) : (
                    <Pins aria-label="Pinned items">
                        {draft.pinned.map((x) => (
                            <li key={`${x.kind}:${x.ref}`}>
                                <Tag>{KINDS.find((k) => k.value === x.kind)?.label ?? x.kind}</Tag>
                                <Link to={pinLink(x)}>{x.label}</Link>
                                <Button size="small" aria-label={`Unpin ${x.label}`}
                                    onClick={() => set({ pinned: draft.pinned.filter((y) => y !== x) })}>Unpin</Button>
                            </li>
                        ))}
                    </Pins>
                )}
                <Space wrap align="end">
                    <Select aria-label="Kind of item to pin" value={pin.kind} style={{ width: 150 }} options={KINDS}
                        onChange={(v) => setPin({ ...pin, kind: v })} />
                    <Input aria-label="What to pin (an urn or id)" placeholder={pin.kind === 'asset' ? 'urn:li:dataset:…' : 'id or name'}
                        style={{ width: 260, maxWidth: '100%' }} value={pin.ref} onChange={(e) => setPin({ ...pin, ref: e.target.value })} />
                    <Input aria-label="Label" placeholder="Label (optional)" style={{ width: 180, maxWidth: '100%' }} value={pin.label}
                        onChange={(e) => setPin({ ...pin, label: e.target.value })} onPressEnter={addPin} />
                    <Button onClick={addPin} disabled={!pin.ref.trim()}>Pin</Button>
                </Space>
            </Section>

            <Section as="section" aria-labelledby="profile-assistant">
                <SectionTitle id="profile-assistant" style={{ marginTop: 0 }}>The assistant’s standing instructions</SectionTitle>
                <p><Muted>How you like answers, e.g. “Start with the number. Use EUR.” Your scheduled tasks follow them too.</Muted></p>
                <Input.TextArea id="profile-instructions" aria-label="Standing instructions" rows={5} maxLength={MAX_INSTRUCTIONS}
                    showCount value={draft.instructions ?? ''} onChange={(e) => set({ instructions: e.target.value })} />
            </Section>

            <Section as="section" aria-labelledby="profile-notifications">
                <SectionTitle id="profile-notifications" style={{ marginTop: 0 }}>Notifications</SectionTitle>
                <Space direction="vertical">
                    {CHANNELS.map((c) => {
                        const disabled = c.key === 'in_app' || (c.key === 'push' && !p.push.enabled);
                        return (
                            <Checkbox key={c.key} checked={!!draft.channels[c.key]} disabled={disabled}
                                onChange={(e) => set({ channels: { ...draft.channels, [c.key]: e.target.checked } })}>
                                {c.label}
                                {c.help && <Muted> ({c.help})</Muted>}
                                {c.key === 'push' && !p.push.enabled && <Muted> (not set up on this Cairn)</Muted>}
                            </Checkbox>
                        );
                    })}
                </Space>
                {p.push.enabled && (
                    <div style={{ marginTop: 16 }}>
                        <strong>This device</strong>
                        <p style={{ margin: '4px 0 8px' }}>
                            <Muted>
                                {!pushSupported()
                                    ? 'This browser cannot receive push notifications.'
                                    : device
                                      ? 'This device gets push notifications for your inbox.'
                                      : `Not getting push notifications here. ${p.push.subscriptions} of your devices do.`}
                            </Muted>
                        </p>
                        {pushSupported() && (
                            <Button onClick={toggleDevice} loading={deviceBusy}>
                                {device ? 'Turn off on this device' : 'Turn on for this device'}
                            </Button>
                        )}
                    </div>
                )}
            </Section>

            <Space>
                <Button type="primary" onClick={save} loading={saving}>Save profile</Button>
                <Button onClick={() => setDraft(draftOf(p))} disabled={saving}>Discard changes</Button>
            </Space>

            <Section as="section" aria-labelledby="profile-devices" style={{ marginTop: 24 }}>
                <SignedInDevices />
            </Section>
        </Page>
    );
}
