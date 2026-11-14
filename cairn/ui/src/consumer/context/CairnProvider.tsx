import React, { createContext, useCallback, useContext, useMemo, useState } from 'react';

import { ConsumerClient } from '../api/client';
import { ConsumerApiProvider, useDheQuery } from '../api/hooks';
import { QueryCache } from '../api/cache';
import type { UiConfig } from '../api/types';

export interface CairnSession {
    config: UiConfig | undefined;
    loading: boolean;
    error: Error | undefined;
    /** Persona the user is currently browsing as (scopes Discover/Trending). Null = all. */
    activePersona: string | null;
    setActivePersona: (id: string | null) => void;
    feature: (name: string, fallback?: boolean) => boolean;
    assistantOpen: boolean;
    setAssistantOpen: (open: boolean) => void;
}

const SessionContext = createContext<CairnSession | null>(null);

const PERSONA_KEY = 'cairn.activePersona';

function readPersona(): string | null {
    try {
        return window.localStorage.getItem(PERSONA_KEY);
    } catch {
        return null;
    }
}

function SessionProvider({ children }: { children?: React.ReactNode }) {
    const cfg = useDheQuery<UiConfig>('/ui/config', (c, signal) => c.getUiConfig({ signal }), { staleMs: 300_000 });
    const [activePersona, setPersonaState] = useState<string | null>(readPersona);
    const [assistantOpen, setAssistantOpen] = useState(false);
    const setActivePersona = useCallback((id: string | null) => {
        setPersonaState(id);
        try {
            if (id) window.localStorage.setItem(PERSONA_KEY, id);
            else window.localStorage.removeItem(PERSONA_KEY);
        } catch {
            // storage unavailable (private mode) — keep in memory
        }
    }, []);
    const value = useMemo<CairnSession>(
        () => ({
            config: cfg.data,
            loading: cfg.loading,
            error: cfg.error,
            activePersona,
            setActivePersona,
            feature: (name, fallback = true) => cfg.data?.features?.[name] ?? fallback,
            assistantOpen,
            setAssistantOpen,
        }),
        [cfg.data, cfg.loading, cfg.error, activePersona, setActivePersona, assistantOpen],
    );
    return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export interface CairnProviderProps {
    client?: ConsumerClient;
    cache?: QueryCache;
    baseUrl?: string;
    graphqlUrl?: string;
    children?: React.ReactNode;
}

/** Root provider for consumer surfaces: API client + cache + session/config. */
export function CairnProvider({ client, cache, baseUrl, graphqlUrl, children }: CairnProviderProps) {
    return (
        <ConsumerApiProvider client={client} cache={cache} baseUrl={baseUrl} graphqlUrl={graphqlUrl}>
            <SessionProvider>{children}</SessionProvider>
        </ConsumerApiProvider>
    );
}

const FALLBACK_SESSION: CairnSession = {
    config: undefined,
    loading: false,
    error: undefined,
    activePersona: null,
    setActivePersona: () => undefined,
    feature: (_n, fallback = true) => fallback,
    assistantOpen: false,
    setAssistantOpen: () => undefined,
};

export function useDheSession(): CairnSession {
    return useContext(SessionContext) ?? FALLBACK_SESSION;
}
