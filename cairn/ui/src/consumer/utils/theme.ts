import { useEffect, useState } from 'react';

/** The workspace's theme (light, dark, or as the system prefers): chosen in the profile menu, kept on this device. The
 * canvas pages (Flows) draw with it. The key is the one the Flows page used before it moved to the profile menu. */
export type Theme = 'light' | 'dark' | 'auto';
export const THEMES: Theme[] = ['light', 'dark', 'auto'];
const KEY = 'cairn.flows.theme';
const EVENT = 'cairn:theme';

export function readTheme(): Theme {
    try {
        const v = window.localStorage.getItem(KEY);
        return THEMES.includes(v as Theme) ? (v as Theme) : 'light';
    } catch {
        return 'light';
    }
}

export function writeTheme(theme: Theme): void {
    try {
        window.localStorage.setItem(KEY, theme);
    } catch {
        // a preference only: storage can be blocked, then it lasts until the page reloads
    }
    window.dispatchEvent(new CustomEvent(EVENT, { detail: theme }));
}

/** The theme, following changes made anywhere on the page. */
export function useTheme(): [Theme, (t: Theme) => void] {
    const [theme, setTheme] = useState<Theme>(readTheme);
    useEffect(() => {
        const on = (e: Event) => setTheme(((e as CustomEvent).detail as Theme) ?? readTheme());
        window.addEventListener(EVENT, on);
        return () => window.removeEventListener(EVENT, on);
    }, []);
    return [theme, (t: Theme) => {
        setTheme(t);
        writeTheme(t);
    }];
}
