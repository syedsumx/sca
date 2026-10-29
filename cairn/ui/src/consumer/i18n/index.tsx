import { ConfigProvider } from 'antd';
import type { Locale as AntLocale } from 'antd/lib/locale-provider';
import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';

import { CATALOGS, LOCALES, Locale, MessageKey, SOURCE } from './messages';

export { LOCALES } from './messages';
export type { Locale, MessageKey } from './messages';

const STORAGE_KEY = 'cairn.locale';
const SUPPORTED = new Set<string>(LOCALES.map((l) => l.code));

/** The saved choice, else the browser's first supported language, else English. */
export function detectLocale(): Locale {
    try {
        const saved = window.localStorage.getItem(STORAGE_KEY);
        if (saved && SUPPORTED.has(saved)) return saved as Locale;
    } catch {
        // storage can be blocked; fall through to the browser's languages
    }
    const langs = typeof navigator !== 'undefined' ? [...(navigator.languages ?? []), navigator.language] : [];
    for (const l of langs) {
        const base = (l || '').toLowerCase().split('-')[0];
        if (SUPPORTED.has(base)) return base as Locale;
    }
    return 'en';
}

export function translate(locale: Locale, key: MessageKey, vars?: Record<string, string | number>): string {
    const text = CATALOGS[locale]?.[key] ?? SOURCE[key] ?? key;
    return vars ? text.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m)) : text;
}

interface I18nValue {
    locale: Locale;
    setLocale: (l: Locale) => void;
    t: (key: MessageKey, vars?: Record<string, string | number>) => string;
}

const I18nContext = createContext<I18nValue>({
    locale: 'en',
    setLocale: () => undefined,
    t: (key, vars) => translate('en', key, vars),
});

export const useI18n = () => useContext(I18nContext);
export const useT = () => useContext(I18nContext).t;

// antd's own texts (pagination, date pickers, empty states), loaded only for the locale in use.
const ANT_LOCALES: Record<Exclude<Locale, 'en'>, () => Promise<{ default: AntLocale }>> = {
    de: () => import('antd/lib/locale/de_DE'),
    fr: () => import('antd/lib/locale/fr_FR'),
    es: () => import('antd/lib/locale/es_ES'),
    ja: () => import('antd/lib/locale/ja_JP'),
};

export function I18nProvider({ initial, children }: { initial?: Locale; children?: React.ReactNode }) {
    const [locale, setLocaleState] = useState<Locale>(() => initial ?? detectLocale());
    const [antLocale, setAntLocale] = useState<AntLocale | undefined>(undefined);
    const setLocale = useCallback((l: Locale) => {
        setLocaleState(l);
        try {
            window.localStorage.setItem(STORAGE_KEY, l);
        } catch {
            // the choice still applies for this visit
        }
    }, []);
    useEffect(() => {
        if (typeof document !== 'undefined') document.documentElement.lang = locale;
        if (locale === 'en') {
            setAntLocale(undefined);
            return;
        }
        let live = true;
        ANT_LOCALES[locale]()
            .then((m) => live && setAntLocale(m.default))
            .catch(() => live && setAntLocale(undefined));
        return () => {
            live = false;
        };
    }, [locale]);
    const value = useMemo<I18nValue>(
        () => ({ locale, setLocale, t: (key, vars) => translate(locale, key, vars) }),
        [locale, setLocale],
    );
    return (
        <I18nContext.Provider value={value}>
            <ConfigProvider locale={antLocale}>{children}</ConfigProvider>
        </I18nContext.Provider>
    );
}
