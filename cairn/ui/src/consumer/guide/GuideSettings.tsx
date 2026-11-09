import { Alert, Switch, message } from 'antd';
import React, { useState } from 'react';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import { Muted, Surface } from '../components/tokens';
import { useT } from '../i18n';

interface UiSettings {
    onboarding: boolean;
    changed_by: string | null;
    changed_ms: number | null;
}

/** Settings → Guided start (admins): the first-run question and the "Get started" checklist, on or off for everyone. */
export function GuideSettings() {
    const { client, cache } = useConsumerApi();
    const t = useT();
    const q = useDheQuery<UiSettings>('/v1/ui/settings', (c, signal) => c.request<UiSettings>('GET', '/v1/ui/settings', undefined, { signal }));
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const set = async (on: boolean) => {
        setBusy(true);
        setError(null);
        try {
            const out = await client.request<UiSettings>('PUT', '/v1/ui/settings', { onboarding: on });
            q.mutate(out);
            cache.invalidate('/ui/config');
            cache.invalidate('/v1/me/capabilities');
            message.success(t('settings.guideSaved', { state: t(on ? 'settings.guideStateOn' : 'settings.guideStateOff') }));
        } catch (e) {
            setError((e as Error).message);
        } finally {
            setBusy(false);
        }
    };
    return (
        <Surface style={{ padding: 20, maxWidth: 720 }}>
            <p><Muted>{t('settings.guideIntro')}</Muted></p>
            {(error || q.error) && <Alert type="error" showIcon message={error ?? q.error?.message} style={{ marginBottom: 12 }} />}
            <label style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <Switch checked={q.data?.onboarding ?? true} loading={busy || q.loading} disabled={!q.data}
                    onChange={(on) => void set(on)} />
                {t('settings.guideOn')}
            </label>
        </Surface>
    );
}
