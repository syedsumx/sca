import { message } from 'antd';
import React, { useCallback } from 'react';
import styled from 'styled-components';

import { useConsumerClient, useDheQuery, useOptimistic } from '../api/hooks';
import type { Bookmark, Follow } from '../api/types';
import { focusRing, tokens } from './tokens';

export const BOOKMARKS_KEY = '/collab/bookmarks';
export const FOLLOWS_KEY = '/collab/follows';

const IconButton = styled.button<{ active?: boolean }>`
    border: 1px solid ${tokens.color.border};
    background: ${(p) => (p.active ? tokens.color.primarySoft : tokens.color.surface)};
    color: ${(p) => (p.active ? tokens.color.primary : tokens.color.text)};
    border-radius: ${tokens.radius.sm};
    padding: 2px 10px;
    font-size: 13px;
    cursor: pointer;
    display: inline-flex;
    gap: 6px;
    align-items: center;
    ${focusRing}
    &:hover {
        border-color: ${tokens.color.primary};
    }
`;

export function useBookmarks() {
    return useDheQuery<Bookmark[]>(BOOKMARKS_KEY, (c, signal) => c.listBookmarks({ signal }));
}

export function useFollows() {
    return useDheQuery<Follow[]>(FOLLOWS_KEY, (c, signal) => c.listFollows({ signal }));
}

/** Toggle a bookmark with optimistic update + rollback. Returns the new state. */
export function useBookmarkToggle() {
    const client = useConsumerClient();
    const run = useOptimistic();
    return useCallback(
        async (urn: string, currentlyBookmarked: boolean) => {
            const next = !currentlyBookmarked;
            try {
                await run<Bookmark[], unknown>(
                    BOOKMARKS_KEY,
                    (prev) =>
                        next
                            ? [{ urn, note: null, created_at: new Date().toISOString() }, ...(prev ?? []).filter((b) => b.urn !== urn)]
                            : (prev ?? []).filter((b) => b.urn !== urn),
                    () => (next ? client.addBookmark(urn) : client.removeBookmark(urn)),
                );
                return next;
            } catch (e) {
                message.error(`Could not ${next ? 'bookmark' : 'remove bookmark'}: ${(e as Error).message}`);
                return currentlyBookmarked;
            }
        },
        [client, run],
    );
}

export function BookmarkToggle({ urn, compact }: { urn: string; compact?: boolean }) {
    const { data } = useBookmarks();
    const toggle = useBookmarkToggle();
    const active = !!data?.some((b) => b.urn === urn);
    return (
        <IconButton
            type="button"
            active={active}
            aria-pressed={active}
            aria-label={active ? 'Remove bookmark' : 'Bookmark'}
            title={active ? 'Remove bookmark (b)' : 'Bookmark (b)'}
            onClick={(e) => {
                e.stopPropagation();
                e.preventDefault();
                void toggle(urn, active);
            }}
        >
            <span aria-hidden>{active ? '★' : '☆'}</span>
            {!compact && (active ? 'Bookmarked' : 'Bookmark')}
        </IconButton>
    );
}

export function FollowButton({ urn }: { urn: string }) {
    const client = useConsumerClient();
    const run = useOptimistic();
    const { data } = useFollows();
    const active = !!data?.some((f) => f.urn === urn);
    const onClick = async () => {
        try {
            await run<Follow[], unknown>(
                FOLLOWS_KEY,
                (prev) =>
                    active
                        ? (prev ?? []).filter((f) => f.urn !== urn)
                        : [{ urn, created_at: new Date().toISOString() }, ...(prev ?? [])],
                () => (active ? client.unfollow(urn) : client.follow(urn)),
            );
        } catch (e) {
            message.error(`Could not update follow: ${(e as Error).message}`);
        }
    };
    return (
        <IconButton type="button" active={active} aria-pressed={active} onClick={onClick}>
            <span aria-hidden>🔔</span>
            {active ? 'Following' : 'Follow'}
        </IconButton>
    );
}

export { IconButton };
