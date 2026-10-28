import { Alert, Button, Empty, Input, List, Popconfirm, Space, Switch, Tabs, Tag, Typography } from 'antd';
import React, { useState } from 'react';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { ExtensionInstallResult, ExtensionSection, InstalledExtension } from '../api/types';
import { Page, SectionTitle } from './tokens';

const SECTIONS = '/extensions/ui';

export function useExtensionSections(placement: ExtensionSection['placement']) {
    const q = useDheQuery<ExtensionSection[]>(SECTIONS, (c, signal) => c.extensionSections({ signal }), {
        staleMs: 300_000,
    });
    return (q.data ?? []).filter((s) => s.placement === placement);
}

/** An extension's page, isolated from Cairn: no cookies, storage or DOM access (no allow-same-origin). */
export function ExtensionFrame({ section, urn }: { section: ExtensionSection; urn?: string }) {
    const src = urn
        ? `${section.url}${section.url.includes('?') ? '&' : '?'}urn=${encodeURIComponent(urn)}`
        : section.url;
    return (
        <iframe
            title={`${section.label} (${section.publisher})`}
            src={src}
            sandbox="allow-scripts allow-forms allow-popups"
            referrerPolicy="no-referrer"
            style={{ width: '100%', minHeight: 640, border: 0, background: 'white' }}
        />
    );
}

export function ExtensionsPage() {
    const sections = useExtensionSections('workspace');
    return (
        <Page>
            <SectionTitle as="h1" style={{ fontSize: 24 }}>
                Extensions
            </SectionTitle>
            {sections.length === 0 ? (
                <Empty description="No extensions add a workspace section." />
            ) : (
                <Tabs
                    items={sections.map((s) => ({
                        key: `${s.extension}:${s.label}`,
                        label: s.label,
                        children: <ExtensionFrame section={s} />,
                    }))}
                />
            )}
        </Page>
    );
}

/** Asset page tabs contributed by extensions (placement: asset). */
export function extensionAssetTabs(sections: ExtensionSection[], urn: string) {
    return sections.map((s) => ({
        key: `ext:${s.extension}:${s.label}`,
        label: s.label,
        children: <ExtensionFrame section={s} urn={urn} />,
    }));
}

/** Settings → Extensions (admins): installed extensions, install from a manifest, token shown once. */
export function ExtensionsAdmin() {
    const { client } = useConsumerApi();
    const q = useDheQuery<InstalledExtension[]>('/admin/extensions', (c, signal) => c.listExtensions({ signal }));
    const [manifest, setManifest] = useState('');
    const [error, setError] = useState<string | null>(null);
    const [installed, setInstalled] = useState<ExtensionInstallResult | null>(null);

    const install = async () => {
        setError(null);
        try {
            setInstalled(await client.installExtension(manifest));
            setManifest('');
            q.refresh();
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        }
    };

    return (
        <section aria-label="Extensions" style={{ maxWidth: 820 }}>
            <List
                dataSource={q.data ?? []}
                locale={{ emptyText: 'No extensions installed.' }}
                renderItem={(x) => (
                    <List.Item
                        actions={[
                            <Switch
                                key="on"
                                aria-label={`Enable ${x.name}`}
                                checked={x.enabled}
                                onChange={async (v) => {
                                    await client.setExtensionEnabled(x.name, v);
                                    q.refresh();
                                }}
                            />,
                            <Popconfirm
                                key="rm"
                                title={`Remove ${x.name}? Its token stops working.`}
                                onConfirm={async () => {
                                    await client.removeExtension(x.name);
                                    q.refresh();
                                }}
                            >
                                <Button danger size="small">
                                    Remove
                                </Button>
                            </Popconfirm>,
                        ]}
                    >
                        <List.Item.Meta
                            title={
                                <span>
                                    {x.name} <Tag>{x.version}</Tag>
                                </span>
                            }
                            description={
                                <>
                                    {x.manifest.publisher}
                                    {x.manifest.description ? ` · ${x.manifest.description}` : ''}
                                    <div>
                                        {x.manifest.permissions.map((p) => (
                                            <Tag key={p}>{p}</Tag>
                                        ))}
                                        {x.manifest.scope ? <Tag color="blue">scoped</Tag> : null}
                                    </div>
                                </>
                            }
                        />
                    </List.Item>
                )}
            />
            {installed?.token && (
                <Alert
                    type="success"
                    showIcon
                    style={{ margin: '16px 0' }}
                    message={`${installed.name} ${installed.version} installed. Copy these now: they are not shown again.`}
                    description={
                        <Space direction="vertical" style={{ width: '100%' }}>
                            <Typography.Text copyable code>
                                {installed.token}
                            </Typography.Text>
                            <Typography.Text copyable code>
                                {installed.signing_secret}
                            </Typography.Text>
                        </Space>
                    }
                />
            )}
            {installed && !installed.token && (
                <Alert
                    type="success"
                    showIcon
                    style={{ margin: '16px 0' }}
                    message={`${installed.name} upgraded from ${installed.upgraded_from} to ${installed.version}; its token is unchanged.`}
                />
            )}
            <Input.TextArea
                aria-label="Extension manifest"
                rows={8}
                placeholder="cairn-extension.yaml"
                value={manifest}
                onChange={(e) => setManifest(e.target.value)}
                style={{ fontFamily: 'monospace', marginTop: 16 }}
            />
            {error && <Alert type="error" showIcon message={error} style={{ marginTop: 8 }} />}
            <Button type="primary" style={{ marginTop: 8 }} disabled={!manifest.trim()} onClick={install}>
                Install or upgrade
            </Button>
        </section>
    );
}
