import { Alert, Button, Input, Result, Space } from 'antd';
import React, { useEffect, useState } from 'react';
import { useLocation } from 'react-router-dom';

import { useConsumerApi } from '../api/hooks';
import type { DeviceCodeInfo } from '../api/types';
import { Muted, Page, Surface, tokens } from '../components/tokens';

/** The code as people type it: letters only, upper case, shown as XXXX-XXXX. */
export function formatCode(raw: string): string {
    const letters = raw.toUpperCase().replace(/[^A-Z]/g, '').slice(0, 8);
    return letters.length > 4 ? `${letters.slice(0, 4)}-${letters.slice(4)}` : letters;
}

/**
 * Approving `cairn login` (the device flow): the person, signed in here, enters (or confirms) the code their terminal
 * shows, sees which device asks, and approves or denies it. Approving gives that device the person's own access.
 */
export function DevicePage() {
    const { client } = useConsumerApi();
    const location = useLocation();
    const fromLink = new URLSearchParams(location.search).get('code') ?? '';
    const [code, setCode] = useState(formatCode(fromLink));
    const [info, setInfo] = useState<DeviceCodeInfo | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [busy, setBusy] = useState(false);
    const [done, setDone] = useState<'approved' | 'denied' | null>(null);

    const lookup = async (value: string) => {
        setBusy(true);
        setError(null);
        try {
            setInfo(await client.deviceCode(value));
        } catch (e) {
            setInfo(null);
            setError((e as Error).message);
        } finally {
            setBusy(false);
        }
    };
    useEffect(() => {
        if (formatCode(fromLink).length === 9) lookup(formatCode(fromLink));
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [fromLink]);

    const decide = async (decision: 'approve' | 'deny') => {
        setBusy(true);
        setError(null);
        try {
            await client.decideDeviceCode(code, decision);
            setDone(decision === 'approve' ? 'approved' : 'denied');
        } catch (e) {
            setError((e as Error).message);
        } finally {
            setBusy(false);
        }
    };

    if (done) {
        return (
            <Page>
                <Result
                    status={done === 'approved' ? 'success' : 'info'}
                    title={done === 'approved' ? 'Device signed in' : 'Sign-in refused'}
                    subTitle={done === 'approved'
                        ? 'You can go back to your terminal. You can sign this device out any time on your profile page.'
                        : 'The device was not signed in. You can close this page.'}
                />
            </Page>
        );
    }
    return (
        <Page>
            <h1>Sign in a device</h1>
            <Surface style={{ padding: tokens.space(5), maxWidth: 560 }}>
                {!info && (
                    <form onSubmit={(e) => { e.preventDefault(); lookup(code); }}>
                        <label htmlFor="device-code" style={{ display: 'block', fontWeight: 600, marginBottom: 4 }}>
                            The code your terminal shows
                        </label>
                        <Space wrap>
                            <Input id="device-code" value={code} autoFocus autoComplete="off" spellCheck={false}
                                placeholder="XXXX-XXXX" style={{ width: 180, fontFamily: 'monospace', fontSize: 18 }}
                                onChange={(e) => setCode(formatCode(e.target.value))} />
                            <Button type="primary" htmlType="submit" loading={busy} disabled={code.length !== 9}>Continue</Button>
                        </Space>
                        <p style={{ marginTop: 12 }}><Muted>Run <code>cairn login</code> in a terminal to get a code.</Muted></p>
                    </form>
                )}
                {info && (
                    <>
                        <p>
                            <strong>{info.name || info.client}</strong> asks to sign in to Cairn as you
                            {info.address ? <> from <code>{info.address}</code></> : null}, at{' '}
                            {new Date(info.requested_ms).toLocaleTimeString()}. Code <code>{code}</code>.
                        </p>
                        <p>
                            <Muted>
                                It will have your access, nothing more, for up to {info.refresh_token_days} days unless you sign it
                                out on your profile page. Only approve a code you started yourself, in your own terminal.
                            </Muted>
                        </p>
                        <Space>
                            <Button type="primary" onClick={() => decide('approve')} loading={busy}>Approve</Button>
                            <Button onClick={() => decide('deny')} disabled={busy}>Deny</Button>
                        </Space>
                    </>
                )}
                {error && <Alert style={{ marginTop: 16 }} type="error" showIcon message={error} />}
            </Surface>
        </Page>
    );
}
