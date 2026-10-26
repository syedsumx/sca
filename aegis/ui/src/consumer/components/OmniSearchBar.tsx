import React, { useEffect, useRef, useState } from 'react';
import { useHistory } from 'react-router-dom';
import styled from 'styled-components';

import { useConsumerClient, useDebounced } from '../api/hooks';
import type { SearchEntity } from '../api/types';
import { useDheSession } from '../context/AegisProvider';
import { entityTypeLabel, platformName } from '../utils/format';
import { serializeSearchState } from '../utils/urlState';
import { useHotkeys } from '../utils/useHotkeys';
import { assetPath } from './Assets';
import { tokens } from './tokens';

const Wrap = styled.div`
    position: relative;
    width: 100%;
`;

const Input = styled.input`
    width: 100%;
    box-sizing: border-box;
    height: 48px;
    padding: 0 110px 0 44px;
    font-size: 16px;
    border-radius: ${tokens.radius.lg};
    border: 1px solid ${tokens.color.border};
    box-shadow: ${tokens.shadow.card};
    outline: none;
    &:focus {
        border-color: ${tokens.color.primary};
        box-shadow: 0 0 0 3px ${tokens.color.primarySoft};
    }
`;

const Icon = styled.span`
    position: absolute;
    left: 16px;
    top: 13px;
    color: ${tokens.color.textMuted};
`;

const Hint = styled.kbd`
    position: absolute;
    right: 14px;
    top: 13px;
    font-size: 12px;
    color: ${tokens.color.textMuted};
    border: 1px solid ${tokens.color.border};
    border-radius: 4px;
    padding: 1px 6px;
    background: ${tokens.color.surfaceAlt};
`;

const List = styled.ul`
    position: absolute;
    z-index: 20;
    top: 54px;
    left: 0;
    right: 0;
    margin: 0;
    padding: 6px;
    list-style: none;
    background: ${tokens.color.surface};
    border: 1px solid ${tokens.color.border};
    border-radius: ${tokens.radius.md};
    box-shadow: ${tokens.shadow.hover};
`;

const Option = styled.li<{ $active: boolean }>`
    padding: 8px 10px;
    border-radius: ${tokens.radius.sm};
    cursor: pointer;
    display: flex;
    justify-content: space-between;
    gap: 12px;
    background: ${(p) => (p.$active ? tokens.color.primarySoft : 'transparent')};
`;

type Suggestion = { kind: 'entity'; entity: SearchEntity } | { kind: 'ask'; text: string } | { kind: 'search'; text: string };

export interface OmniSearchBarProps {
    initialQuery?: string;
    autoFocus?: boolean;
    placeholder?: string;
    /** Called instead of navigation when the user picks "Ask the assistant". */
    onAsk?: (question: string) => void;
}

/**
 * Typeahead search: entities as you type, plus "Search all" and "Ask the assistant" rows.
 * `/` or ⌘K focuses it from anywhere.
 */
export function OmniSearchBar({ initialQuery = '', autoFocus, placeholder, onAsk }: OmniSearchBarProps) {
    const client = useConsumerClient();
    const history = useHistory();
    const session = useDheSession();
    const [text, setText] = useState(initialQuery);
    const [open, setOpen] = useState(false);
    const [active, setActive] = useState(0);
    const [entities, setEntities] = useState<SearchEntity[]>([]);
    const inputRef = useRef<HTMLInputElement>(null);
    const query = useDebounced(text.trim(), 180);

    useHotkeys({ '/': () => inputRef.current?.focus(), 'mod+k': () => inputRef.current?.focus() });

    useEffect(() => {
        if (query.length < 2) {
            setEntities([]);
            return undefined;
        }
        const controller = new AbortController();
        client
            .search({ query, filters: {}, count: 6 }, controller.signal)
            .then((r) => setEntities(r.results))
            .catch(() => setEntities([]));
        return () => controller.abort();
    }, [client, query]);

    const suggestions: Suggestion[] = text.trim()
        ? [
              ...entities.map((entity) => ({ kind: 'entity' as const, entity })),
              { kind: 'search', text: text.trim() },
              { kind: 'ask', text: text.trim() },
          ]
        : [];

    const pick = (s: Suggestion | undefined) => {
        setOpen(false);
        if (!s) return;
        if (s.kind === 'entity') history.push(assetPath(s.entity.urn));
        else if (s.kind === 'search') history.push(`/search${serializeSearchState({ query: s.text, filters: {}, page: 1 })}`);
        else if (onAsk) onAsk(s.text);
        else {
            session.setAssistantOpen(true);
            history.push(`/assistant?q=${encodeURIComponent(s.text)}`);
        }
    };

    const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            setOpen(true);
            setActive((a) => Math.min(a + 1, suggestions.length - 1));
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            setActive((a) => Math.max(a - 1, 0));
        } else if (e.key === 'Enter') {
            e.preventDefault();
            // Enter without navigating the list = full search
            pick(open && active < entities.length ? suggestions[active] : { kind: 'search', text: text.trim() });
        } else if (e.key === 'Escape') {
            setOpen(false);
        }
    };

    const listId = 'aegis-omnisearch-list';
    return (
        <Wrap>
            <Icon aria-hidden>🔍</Icon>
            <Input
                ref={inputRef}
                role="combobox"
                aria-expanded={open && suggestions.length > 0}
                aria-controls={listId}
                aria-autocomplete="list"
                aria-activedescendant={open && suggestions.length ? `${listId}-${active}` : undefined}
                aria-label="Search data or ask a question"
                // eslint-disable-next-line jsx-a11y/no-autofocus
                autoFocus={autoFocus}
                placeholder={placeholder ?? 'Search datasets, dashboards, metrics… or ask a question'}
                value={text}
                onChange={(e) => {
                    setText(e.target.value);
                    setOpen(true);
                    setActive(0);
                }}
                onFocus={() => setOpen(true)}
                onBlur={() => setTimeout(() => setOpen(false), 120)}
                onKeyDown={onKeyDown}
            />
            <Hint aria-hidden>/</Hint>
            {open && suggestions.length > 0 && (
                <List id={listId} role="listbox">
                    {suggestions.map((s, i) => (
                        <Option
                            key={s.kind === 'entity' ? s.entity.urn : s.kind}
                            id={`${listId}-${i}`}
                            role="option"
                            aria-selected={i === active}
                            $active={i === active}
                            onMouseEnter={() => setActive(i)}
                            onMouseDown={(e) => {
                                e.preventDefault();
                                pick(s);
                            }}
                        >
                            {s.kind === 'entity' && (
                                <>
                                    <span>{s.entity.name}</span>
                                    <span style={{ color: tokens.color.textMuted, fontSize: 12 }}>
                                        {entityTypeLabel(s.entity.type)}
                                        {s.entity.platform ? ` · ${platformName(s.entity.platform)}` : ''}
                                    </span>
                                </>
                            )}
                            {s.kind === 'search' && (
                                <span>
                                    Search all results for <strong>“{s.text}”</strong>
                                </span>
                            )}
                            {s.kind === 'ask' && (
                                <span>
                                    ✨ Ask the assistant: <strong>“{s.text}”</strong>
                                </span>
                            )}
                        </Option>
                    ))}
                </List>
            )}
        </Wrap>
    );
}
