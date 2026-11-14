import React, { Suspense, useCallback, useEffect, useRef, useState } from 'react';
import styled from 'styled-components';

import { Icon } from '../components/Icons';
import { focusRing, tokens } from '../components/tokens';
import { useT } from '../i18n';
import { useHotkeys } from '../utils/useHotkeys';
import type { PaletteNavEntry, SearchPanelHandle } from './CommandPalette';
import { useGuide } from './GuideProvider';

// The results (pages, tabs, actions and the catalog's answers) load on first use: they read the areas' catalog.
const SearchPanel = React.lazy(() => import('./CommandPalette').then((m) => ({ default: m.SearchPanel })));
const LIST_ID = 'cairn-search-list';

/** The search shortcut as this computer writes it. */
export function paletteKeys(platform: string = typeof navigator !== 'undefined' ? navigator.platform || '' : ''): string {
    return /mac|iphone|ipad/i.test(platform) ? '⌘ K' : 'Ctrl K';
}

const Wrap = styled.div`
    position: relative;
    flex: 1 1 260px;
    max-width: 560px;
    min-width: 0;
    .box {
        display: flex;
        align-items: center;
        gap: 8px;
        min-height: ${tokens.control}px;
        padding: 0 10px 0 12px;
        border: 1px solid ${tokens.color.borderStrong};
        border-radius: ${tokens.radius.md};
        background: ${tokens.color.surface};
        color: ${tokens.color.textMuted};
    }
    .box:hover {
        border-color: ${tokens.color.primary};
    }
    .box:focus-within {
        border-color: ${tokens.color.primary};
        box-shadow: 0 0 0 3px ${tokens.color.primarySoft};
    }
    input {
        flex: 1;
        min-width: 0;
        border: 0;
        outline: none;
        background: transparent;
        font: inherit;
        font-size: 14px;
        color: ${tokens.color.text};
        min-height: ${tokens.control - 2}px;
    }
    input::placeholder {
        color: #56606f;
    }
    kbd {
        font: 12px ${tokens.font};
        color: ${tokens.color.textMuted};
        border: 1px solid ${tokens.color.borderStrong};
        border-radius: 5px;
        padding: 0 6px;
        background: ${tokens.color.surface};
        white-space: nowrap;
    }
    .toggle,
    .close {
        display: none;
    }
    /* On a phone the box is an icon in the header; it opens over the header's row. */
    @media (max-width: 720px) {
        flex: none;
        .box {
            display: none;
        }
        .toggle {
            display: inline-grid;
            place-items: center;
            width: ${tokens.control}px;
            height: ${tokens.control}px;
            border: 0;
            border-radius: ${tokens.radius.sm};
            background: transparent;
            color: ${tokens.color.text};
            cursor: pointer;
            ${focusRing}
        }
        &[data-open='true'] {
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            z-index: 1040;
            max-width: none;
            padding: 8px 12px;
            background: ${tokens.color.surface};
            border-bottom: 1px solid ${tokens.color.border};
            display: flex;
            gap: 8px;
            align-items: center;
            .box {
                display: flex;
                flex: 1;
            }
            .toggle,
            kbd {
                display: none;
            }
            .close {
                display: inline-grid;
                place-items: center;
                width: ${tokens.control}px;
                height: ${tokens.control}px;
                border: 0;
                background: transparent;
                cursor: pointer;
                color: ${tokens.color.text};
                ${focusRing}
            }
        }
    }
`;

/**
 * The one search in the header (L150): typing searches the catalog and also finds pages, tabs and actions (the
 * command palette's behaviour), with "Search all data" and "Ask" at the end. Ctrl K (⌘ K) and / focus it from anywhere;
 * other parts of the workspace open it through the guide (`setPalette(true)`). On a phone it is an icon that opens it
 * over the header's row.
 */
export function HeaderSearch({ nav }: { nav: PaletteNavEntry[] }) {
    const t = useT();
    const guide = useGuide();
    const input = useRef<HTMLInputElement>(null);
    const panel = useRef<SearchPanelHandle>(null);
    const [q, setQ] = useState('');
    const [open, setOpen] = useState(false);
    const [phoneOpen, setPhoneOpen] = useState(false);
    const [activeId, setActiveId] = useState<string | undefined>(undefined);
    const [count, setCount] = useState(0);
    const [loaded, setLoaded] = useState(false);

    const focus = useCallback(() => {
        setPhoneOpen(true);
        setOpen(true);
        setLoaded(true);
        setTimeout(() => input.current?.focus(), 0);
    }, []);
    // Ctrl K (and the places that "open the palette": Get started, the shortcuts sheet) focus the box.
    useEffect(() => {
        if (guide.palette) {
            focus();
            guide.setPalette(false);
        }
    }, [guide, focus]);
    useHotkeys({
        '/': () => {
            // A page's own search box (Discover, Catalog) takes it first; elsewhere, the header's.
            if (document.activeElement?.tagName !== 'INPUT') focus();
        },
    });

    const close = () => {
        setOpen(false);
        setPhoneOpen(false);
    };
    const chosen = useCallback(() => {
        setOpen(false);
        setPhoneOpen(false);
        setQ('');
        input.current?.blur();
    }, []);
    const expanded = open && loaded && count > 0;
    return (
        <Wrap className="cairn-search" data-open={phoneOpen}>
            <button type="button" className="toggle" aria-label={t('search.label')} aria-keyshortcuts="Control+K Meta+K /"
                onClick={focus}>
                <Icon name="search" size={20} />
            </button>
            <div className="box">
                <Icon name="search" size={16} />
                <input
                    ref={input}
                    type="text"
                    role="combobox"
                    aria-label={t('search.label')}
                    aria-expanded={expanded}
                    aria-controls={expanded ? LIST_ID : undefined}
                    aria-autocomplete="list"
                    aria-activedescendant={expanded ? activeId : undefined}
                    aria-keyshortcuts="Control+K Meta+K /"
                    placeholder={t('search.placeholder')}
                    title={t('nav.searchShortcut', { keys: paletteKeys() })}
                    value={q}
                    autoComplete="off"
                    spellCheck={false}
                    onChange={(e) => {
                        setQ(e.target.value);
                        setOpen(true);
                        setLoaded(true);
                    }}
                    onFocus={() => {
                        setOpen(true);
                        setLoaded(true);
                    }}
                    // Closes after a click on an entry has been handled, unless the box has the focus again by then.
                    onBlur={() => setTimeout(() => {
                        if (document.activeElement !== input.current) setOpen(false);
                    }, 150)}
                    onKeyDown={(e) => {
                        if (e.key === 'Escape') {
                            e.preventDefault();
                            if (open) setOpen(false);
                            else {
                                setQ('');
                                close();
                                input.current?.blur();
                            }
                            return;
                        }
                        if (!open) setOpen(true);
                        panel.current?.onKey(e);
                    }}
                />
                <kbd aria-hidden="true">{paletteKeys()}</kbd>
            </div>
            <button type="button" className="close" aria-label={t('search.close')} onClick={() => {
                setQ('');
                close();
            }}>
                <Icon name="close" size={20} />
            </button>
            {loaded && open && (
                <Suspense fallback={null}>
                    <SearchPanel ref={panel} nav={nav} q={q} setQ={setQ} onChosen={chosen} onActive={setActiveId} onCount={setCount} />
                </Suspense>
            )}
        </Wrap>
    );
}
