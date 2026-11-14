import { useEffect, useRef } from 'react';

export type HotkeyMap = Record<string, (e: KeyboardEvent) => void>;

function isEditable(target: EventTarget | null): boolean {
    const el = target as HTMLElement | null;
    if (!el || !el.tagName) return false;
    const tag = el.tagName.toLowerCase();
    return tag === 'input' || tag === 'textarea' || tag === 'select' || el.isContentEditable;
}

/**
 * Global single-key shortcuts (`/`, `j`, `k`, `b`, `mod+k`). Ignored while typing in form fields,
 * except `Escape` and `mod+k`.
 */
export function useHotkeys(map: HotkeyMap, enabled = true): void {
    const ref = useRef(map);
    ref.current = map;
    useEffect(() => {
        if (!enabled) return undefined;
        const onKey = (e: KeyboardEvent) => {
            const mod = e.metaKey || e.ctrlKey;
            const combo = `${mod ? 'mod+' : ''}${e.key.toLowerCase()}`;
            const handler = ref.current[combo] ?? (!mod ? ref.current[e.key] : undefined);
            if (!handler) return;
            if (isEditable(e.target) && combo !== 'mod+k' && e.key !== 'Escape') return;
            e.preventDefault();
            handler(e);
        };
        window.addEventListener('keydown', onKey);
        return () => window.removeEventListener('keydown', onKey);
    }, [enabled]);
}
