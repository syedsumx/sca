import { Alert, Button, Empty, Popconfirm, message } from 'antd';
import React, { useState } from 'react';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { SignedInDevice } from '../api/types';
import { Muted, SectionTitle, tokens } from './tokens';

function when(ms: number): string {
    return new Date(ms).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

/** Devices signed in with `cairn login` (the command line), each with a sign-out that stops its tokens at once. */
export function SignedInDevices() {
    const { client } = useConsumerApi();
    const devices = useDheQuery<{ devices: SignedInDevice[] }>('/v1/me/devices', (c, signal) => c.myDevices({ signal }));
    const [busy, setBusy] = useState<string | null>(null);

    const signOut = async (d: SignedInDevice) => {
        setBusy(d.id);
        try {
            await client.signOutDevice(d.id);
            devices.mutate((prev) => ({ devices: (prev?.devices ?? []).filter((x) => x.id !== d.id) }));
            message.success(`Signed out ${d.name}`);
        } catch (e) {
            message.error((e as Error).message);
        } finally {
            setBusy(null);
        }
    };

    return (
        <>
            <SectionTitle id="profile-devices" style={{ marginTop: 0 }}>Signed-in devices</SectionTitle>
            <p>
                <Muted>
                    Computers where you ran <code>cairn login</code>. They act as you, with your access. Sign one out
                    and its tokens stop working at once.
                </Muted>
            </p>
            {devices.error && <Alert type="error" showIcon message={devices.error.message} />}
            {devices.data && devices.data.devices.length === 0 && (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No devices are signed in with the command line" />
            )}
            {devices.data && devices.data.devices.length > 0 && (
                <ul aria-label="Signed-in devices" style={{ listStyle: 'none', margin: 0, padding: 0 }}>
                    {devices.data.devices.map((d) => (
                        <li key={d.id} style={{ display: 'flex', gap: tokens.space(2), alignItems: 'center', flexWrap: 'wrap',
                            padding: `${tokens.space(2)} 0`, borderBottom: `1px solid ${tokens.color.border}` }}>
                            <span style={{ flex: 1, minWidth: 0, overflowWrap: 'anywhere' }}>
                                <strong>{d.name}</strong>
                                <br />
                                <Muted>
                                    Signed in {when(d.signed_in_ms)} · last used {when(d.last_used_ms)} · until{' '}
                                    {when(d.expires_ms)}
                                </Muted>
                            </span>
                            <Popconfirm title={`Sign out ${d.name}?`} okText="Sign out" onConfirm={() => signOut(d)}>
                                <Button danger size="small" loading={busy === d.id} aria-label={`Sign out ${d.name}`}>
                                    Sign out
                                </Button>
                            </Popconfirm>
                        </li>
                    ))}
                </ul>
            )}
        </>
    );
}
