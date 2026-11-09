import { message } from 'antd';
import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { Profile } from '../api/types';
import { useDheSession } from '../context/CairnProvider';
import { useT } from '../i18n';
import { CapabilityState, useCapabilities } from './capabilities';
import {
    EMPTY_UI, GuideUi, Role, detectDone, normaliseUi, roleFromProfile, stepsForEvent, stepsVisited, withDone,
} from './onboarding';

export interface GuidePatch {
    onboarding?: Partial<GuideUi['onboarding']>;
    sidebar?: Partial<GuideUi['sidebar']>;
}

export interface GuideValue {
    ui: GuideUi;
    /** The profile answered: the person's guide state is known and can be saved. */
    available: boolean;
    /** The workspace offers onboarding (an admin can turn it off). */
    enabled: boolean;
    caps: CapabilityState;
    me: string | undefined;
    /** The role the profile's own role suggests (for the first-run question). */
    suggestedRole: Role | null;
    save: (patch: GuidePatch) => Promise<void>;
    markDone: (ids: string[]) => void;
    toggleDone: (id: string) => void;
    /** Ask the API now which steps are done (the checklist's "Check again"). */
    detect: () => Promise<void>;
    palette: boolean;
    setPalette: (open: boolean) => void;
    shortcuts: boolean;
    setShortcuts: (open: boolean) => void;
    help: boolean;
    setHelp: (open: boolean) => void;
    customise: boolean;
    setCustomise: (open: boolean) => void;
}

const noop = () => undefined;
const FALLBACK: GuideValue = {
    ui: EMPTY_UI, available: false, enabled: false, caps: { caps: undefined, loading: false, error: true }, me: undefined,
    suggestedRole: null, save: async () => undefined, markDone: noop, toggleDone: noop, detect: async () => undefined,
    palette: false, setPalette: noop, shortcuts: false, setShortcuts: noop, help: false, setHelp: noop, customise: false,
    setCustomise: noop,
};

const GuideContext = createContext<GuideValue>(FALLBACK);
export const useGuide = () => useContext(GuideContext);

const PROFILE_KEY = '/v1/me/profile';
const DETECT_EVERY_MS = 15_000;

/** The guided experience's state for the signed-in person: their onboarding and sidebar (kept in their profile), their
 * capabilities, and which of the palette, help and shortcut sheets is open. */
export function GuideProvider({ children }: { children?: React.ReactNode }) {
    const { client, cache } = useConsumerApi();
    const session = useDheSession();
    const t = useT();
    const location = useLocation();
    const profile = useDheQuery<Profile>(PROFILE_KEY, (c, signal) => c.profile({ signal }), { staleMs: 300_000 });
    const caps = useCapabilities();
    const ui = useMemo(() => normaliseUi(profile.data?.ui), [profile.data]);
    const uiRef = useRef(ui);
    uiRef.current = ui;
    const available = !!profile.data;
    const enabled = available && session.feature('onboarding', true) && caps.caps?.features?.onboarding !== false;
    const me = session.config?.user?.urn ?? profile.data?.urn;
    const [palette, setPalette] = useState(false);
    const [shortcuts, setShortcuts] = useState(false);
    const [help, setHelp] = useState(false);
    const [customise, setCustomise] = useState(false);

    const save = useCallback(async (patch: GuidePatch) => {
        const before = cache.get<Profile>(PROFILE_KEY)?.data;
        if (!before) return;
        const cur = uiRef.current;
        const next: GuideUi = {
            onboarding: { ...cur.onboarding, ...(patch.onboarding ?? {}) },
            sidebar: { ...cur.sidebar, ...(patch.sidebar ?? {}) },
        };
        uiRef.current = next;
        cache.set<Profile>(PROFILE_KEY, { ...before, ui: next });
        try {
            const out = await client.request<Profile>('PUT', PROFILE_KEY, { ui: patch });
            cache.set<Profile>(PROFILE_KEY, out);
        } catch (e) {
            cache.set<Profile>(PROFILE_KEY, before);
            uiRef.current = normaliseUi(before.ui);
            message.error(t('guide.saveFailed', { reason: (e as Error).message }));
        }
    }, [cache, client, t]);

    const markDone = useCallback((ids: string[]) => {
        const done = withDone(uiRef.current.onboarding.done, ids);
        if (done !== uiRef.current.onboarding.done) void save({ onboarding: { done } });
    }, [save]);

    const toggleDone = useCallback((id: string) => {
        const cur = uiRef.current.onboarding.done;
        void save({ onboarding: { done: cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id] } });
    }, [save]);

    const lastDetect = useRef(0);
    const detect = useCallback(async () => {
        const { role, done } = uiRef.current.onboarding;
        if (!me || !role) return;
        lastDetect.current = Date.now();
        const found = await detectDone(client, me, role, done);
        if (found.length) markDone(found);
    }, [client, me, markDone]);

    // A visit completes some steps; other steps are asked of the API now and then, as the person moves around.
    const role = ui.onboarding.role;
    useEffect(() => {
        if (!enabled || !role) return;
        const visited = stepsVisited(role, location.pathname);
        if (visited.length) markDone(visited);
        if (Date.now() - lastDetect.current > DETECT_EVERY_MS) void detect();
    }, [enabled, role, location.pathname, markDone, detect]);

    useEffect(() => {
        if (!enabled || !role) return undefined;
        const on = (e: Event) => {
            const ids = stepsForEvent(role, String((e as CustomEvent).detail ?? ''));
            if (ids.length) markDone(ids);
        };
        window.addEventListener('cairn:guide', on);
        return () => window.removeEventListener('cairn:guide', on);
    }, [enabled, role, markDone]);

    const value = useMemo<GuideValue>(() => ({
        ui, available, enabled, caps, me, suggestedRole: roleFromProfile(profile.data?.role), save, markDone, toggleDone,
        detect, palette, setPalette, shortcuts, setShortcuts, help, setHelp, customise, setCustomise,
    }), [ui, available, enabled, caps, me, profile.data?.role, save, markDone, toggleDone, detect, palette, shortcuts, help,
        customise]);
    return <GuideContext.Provider value={value}>{children}</GuideContext.Provider>;
}
