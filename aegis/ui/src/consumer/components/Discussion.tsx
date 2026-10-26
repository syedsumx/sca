import { Avatar, Button, Empty, Input, Tag, message } from 'antd';
import React, { useEffect, useRef, useState } from 'react';
import styled from 'styled-components';

import { useConsumerApi, useDheQuery, useOptimistic } from '../api/hooks';
import type { Comment, Thread } from '../api/types';
import { actorName, relativeTime } from '../utils/format';
import { MarkdownView, extractMentions } from '../utils/markdown';
import { Skeleton, tokens } from './tokens';

const REACTIONS = ['👍', '🎉', '👀', '❓'];

export function threadsKey(entityUrn: string): string {
    return `/collab/threads?entity_urn=${entityUrn}`;
}

const CommentBox = styled.div`
    display: grid;
    grid-template-columns: 32px 1fr;
    gap: 10px;
    padding: 10px 0;
`;

const Reaction = styled.button<{ $mine: boolean }>`
    border: 1px solid ${(p) => (p.$mine ? tokens.color.primary : tokens.color.border)};
    background: ${(p) => (p.$mine ? tokens.color.primarySoft : tokens.color.surface)};
    border-radius: 999px;
    padding: 0 8px;
    margin-right: 6px;
    font-size: 12px;
    cursor: pointer;
`;

export function CommentView({
    comment,
    me,
    onReact,
}: {
    comment: Comment;
    me?: string;
    onReact: (emoji: string, add: boolean) => void;
}) {
    return (
        <CommentBox>
            <Avatar size={32} aria-hidden>
                {actorName(comment.author).charAt(0).toUpperCase()}
            </Avatar>
            <div>
                <div style={{ fontSize: 13 }}>
                    <strong>{actorName(comment.author)}</strong>{' '}
                    <span style={{ color: tokens.color.textMuted }}>
                        {relativeTime(comment.created_at)}
                        {comment.edited_at ? ' · edited' : ''}
                    </span>
                </div>
                <MarkdownView source={comment.body_md} />
                <div role="group" aria-label="Reactions">
                    {REACTIONS.map((emoji) => {
                        const actors = comment.reactions?.[emoji] ?? [];
                        const mine = !!me && actors.includes(me);
                        if (!actors.length && !mine) {
                            return (
                                <Reaction
                                    key={emoji}
                                    type="button"
                                    $mine={false}
                                    aria-label={`React ${emoji}`}
                                    onClick={() => onReact(emoji, true)}
                                    style={{ opacity: 0.55 }}
                                >
                                    {emoji}
                                </Reaction>
                            );
                        }
                        return (
                            <Reaction
                                key={emoji}
                                type="button"
                                $mine={mine}
                                aria-pressed={mine}
                                aria-label={`${emoji} ${actors.length}`}
                                onClick={() => onReact(emoji, !mine)}
                            >
                                {emoji} {actors.length}
                            </Reaction>
                        );
                    })}
                </div>
            </div>
        </CommentBox>
    );
}

/** Markdown textarea with @mention typeahead (DataHub users via search). */
export function CommentComposer({
    onSubmit,
    placeholder,
    submitLabel = 'Comment',
}: {
    onSubmit: (body: string, mentions: string[]) => Promise<void> | void;
    placeholder?: string;
    submitLabel?: string;
}) {
    const { client } = useConsumerApi();
    const [text, setText] = useState('');
    const [busy, setBusy] = useState(false);
    const [suggestions, setSuggestions] = useState<string[]>([]);
    const mentionQuery = text.match(/@([A-Za-z0-9._-]{1,})$/)?.[1] ?? null;
    const ref = useRef<string | null>(null);

    useEffect(() => {
        ref.current = mentionQuery;
        if (!mentionQuery) {
            setSuggestions([]);
            return undefined;
        }
        const controller = new AbortController();
        client
            .search({ query: mentionQuery, filters: { _entityType: ['CORP_USER'] }, count: 5 }, controller.signal)
            .then((r) => {
                if (ref.current === mentionQuery) setSuggestions(r.results.map((u) => actorName(u.urn)));
            })
            .catch(() => setSuggestions([]));
        return () => controller.abort();
    }, [client, mentionQuery]);

    const submit = async () => {
        const body = text.trim();
        if (!body) return;
        setBusy(true);
        try {
            await onSubmit(body, extractMentions(body));
            setText('');
        } finally {
            setBusy(false);
        }
    };

    return (
        <div>
            <Input.TextArea
                aria-label={placeholder ?? 'Write a comment'}
                placeholder={placeholder ?? 'Ask a question or share context… Use @ to mention, Markdown supported'}
                autoSize={{ minRows: 2, maxRows: 8 }}
                value={text}
                onChange={(e) => setText(e.target.value)}
                onKeyDown={(e) => {
                    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                        e.preventDefault();
                        void submit();
                    }
                }}
            />
            {suggestions.length > 0 && (
                <div role="listbox" aria-label="Mention suggestions" style={{ marginTop: 4 }}>
                    {suggestions.map((s) => (
                        <Tag
                            key={s}
                            role="option"
                            style={{ cursor: 'pointer' }}
                            onClick={() => setText(text.replace(/@([A-Za-z0-9._-]+)$/, `@${s} `))}
                        >
                            @{s}
                        </Tag>
                    ))}
                </div>
            )}
            <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 6 }}>
                <Button type="primary" onClick={() => void submit()} loading={busy} disabled={!text.trim()}>
                    {submitLabel}
                </Button>
            </div>
        </div>
    );
}

export function ThreadView({ thread, me }: { thread: Thread; me?: string }) {
    const { client, cache } = useConsumerApi();
    const run = useOptimistic();
    const key = threadsKey(thread.entity_urn);

    const react = (comment: Comment, emoji: string, add: boolean) =>
        run<Thread[], unknown>(
            key,
            (prev) =>
                (prev ?? []).map((t) =>
                    t.id !== thread.id
                        ? t
                        : {
                              ...t,
                              comments: t.comments.map((c) => {
                                  if (c.id !== comment.id) return c;
                                  const actors = new Set(c.reactions?.[emoji] ?? []);
                                  if (add && me) actors.add(me);
                                  if (!add && me) actors.delete(me);
                                  return { ...c, reactions: { ...c.reactions, [emoji]: Array.from(actors) } };
                              }),
                          },
                ),
            () => client.toggleReaction(comment.id, emoji, add),
        ).catch((e: Error) => message.error(`Reaction failed: ${e.message}`));

    const reply = async (body: string) => {
        try {
            const created = await client.addComment(thread.id, body);
            cache.set<Thread[]>(key, (prev) =>
                (prev ?? []).map((t) => (t.id === thread.id ? { ...t, comments: [...t.comments, created] } : t)),
            );
        } catch (e) {
            message.error(`Could not post reply: ${(e as Error).message}`);
            throw e;
        }
    };

    const setStatus = async () => {
        try {
            const updated = await client.setThreadStatus(thread.id, thread.status === 'OPEN' ? 'resolve' : 'reopen');
            cache.set<Thread[]>(key, (prev) => (prev ?? []).map((t) => (t.id === thread.id ? updated : t)));
        } catch (e) {
            message.error((e as Error).message);
        }
    };

    return (
        <article
            aria-label={`Discussion thread${thread.field_path ? ` on column ${thread.field_path}` : ''}`}
            style={{
                border: `1px solid ${tokens.color.border}`,
                borderRadius: tokens.radius.md,
                padding: '8px 14px',
                marginBottom: 12,
                background: thread.status === 'RESOLVED' ? tokens.color.surfaceAlt : tokens.color.surface,
            }}
        >
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <div>
                    {thread.field_path && <Tag color="purple">column: {thread.field_path}</Tag>}
                    <Tag color={thread.status === 'OPEN' ? 'blue' : 'default'}>{thread.status === 'OPEN' ? 'Open' : 'Resolved'}</Tag>
                </div>
                <Button size="small" onClick={() => void setStatus()}>
                    {thread.status === 'OPEN' ? 'Mark resolved' : 'Reopen'}
                </Button>
            </div>
            {thread.comments.map((c) => (
                <CommentView key={c.id} comment={c} me={me} onReact={(emoji, add) => void react(c, emoji, add)} />
            ))}
            {thread.status === 'OPEN' && <CommentComposer onSubmit={reply} submitLabel="Reply" placeholder="Reply…" />}
        </article>
    );
}

export function DiscussionPanel({ entityUrn, me, fieldPath }: { entityUrn: string; me?: string; fieldPath?: string | null }) {
    const { client, cache } = useConsumerApi();
    const key = threadsKey(entityUrn);
    const { data, loading, error } = useDheQuery<Thread[]>(key, (c, signal) => c.listThreads(entityUrn, { signal }));
    const threads = (data ?? []).filter((t) => fieldPath === undefined || t.field_path === fieldPath);

    const start = async (body: string) => {
        try {
            const created = await client.createThread(entityUrn, body, fieldPath ?? null);
            cache.set<Thread[]>(key, (prev) => [created, ...(prev ?? [])]);
        } catch (e) {
            message.error(`Could not start discussion: ${(e as Error).message}`);
            throw e;
        }
    };

    return (
        <section aria-label="Discussion">
            <CommentComposer onSubmit={start} submitLabel="Start discussion" />
            <div style={{ marginTop: 16 }}>
                {loading && (
                    <>
                        <Skeleton h={60} />
                        <div style={{ height: 8 }} />
                        <Skeleton h={60} />
                    </>
                )}
                {error && <div role="alert">Could not load discussions: {error.message}</div>}
                {!loading && !error && threads.length === 0 && (
                    <Empty description="No discussions yet — ask the owners a question or share context." />
                )}
                {threads.map((t) => (
                    <ThreadView key={t.id} thread={t} me={me} />
                ))}
            </div>
        </section>
    );
}
