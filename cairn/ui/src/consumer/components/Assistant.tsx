import { Alert, Button, Drawer, Input, Tag } from 'antd';
import React, { useEffect, useRef, useState } from 'react';
import { useHistory } from 'react-router-dom';
import styled from 'styled-components';

import { useConsumerApi } from '../api/hooks';
import type { ChatResponse, SuggestedAction } from '../api/types';
import { useDheSession } from '../context/CairnProvider';
import { shortUrn } from '../utils/format';
import { MarkdownView } from '../utils/markdown';
import { assetPath } from './Assets';
import { tokens } from './tokens';

export interface ChatTurn {
    role: 'user' | 'assistant';
    text: string;
    response?: ChatResponse;
    error?: string;
}

const Bubble = styled.div<{ $user: boolean }>`
    max-width: 92%;
    align-self: ${(p) => (p.$user ? 'flex-end' : 'flex-start')};
    background: ${(p) => (p.$user ? tokens.color.primarySoft : tokens.color.surface)};
    border: 1px solid ${(p) => (p.$user ? 'transparent' : tokens.color.border)};
    border-radius: ${tokens.radius.md};
    padding: 8px 12px;
    font-size: 14px;
`;

const Log = styled.div`
    display: flex;
    flex-direction: column;
    gap: 10px;
    overflow-y: auto;
    flex: 1;
    padding: 4px 2px 12px;
`;

const STARTERS = [
    'Which certified tables contain customer data?',
    'Why did revenue_kpi drop yesterday?',
    'What does “active customer” mean and where is it defined?',
];

/** Conversational assistant over metadata (POST /ai/chat). Used full-page and in the slide-over. */
export function AssistantChat({ initialQuestion, compact }: { initialQuestion?: string; compact?: boolean }) {
    const { client } = useConsumerApi();
    const history = useHistory();
    const [turns, setTurns] = useState<ChatTurn[]>([]);
    const [conversationId, setConversationId] = useState<string | null>(null);
    const [text, setText] = useState('');
    const [busy, setBusy] = useState(false);
    const logRef = useRef<HTMLDivElement>(null);
    const asked = useRef(false);

    const send = async (message: string) => {
        const q = message.trim();
        if (!q || busy) return;
        setTurns((t) => [...t, { role: 'user', text: q }]);
        setText('');
        setBusy(true);
        try {
            const res = await client.chat(q, conversationId);
            setConversationId(res.conversation_id);
            setTurns((t) => [...t, { role: 'assistant', text: res.reply_md, response: res }]);
        } catch (e) {
            setTurns((t) => [...t, { role: 'assistant', text: '', error: (e as Error).message }]);
        } finally {
            setBusy(false);
        }
    };

    useEffect(() => {
        if (initialQuestion && !asked.current) {
            asked.current = true;
            void send(initialQuestion);
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [initialQuestion]);

    useEffect(() => {
        logRef.current?.scrollTo?.({ top: logRef.current.scrollHeight });
    }, [turns]);

    const act = async (a: SuggestedAction) => {
        if (a.kind === 'open') history.push(assetPath(a.urn));
        else if (a.kind === 'follow') await client.follow(a.urn).catch(() => undefined);
        else if (a.kind === 'request_access') history.push(`/marketplace/${encodeURIComponent(a.urn)}`);
        else if (a.kind === 'explain') void send(`Explain the lineage anomaly for ${a.urn}`);
    };

    return (
        <div style={{ display: 'flex', flexDirection: 'column', height: compact ? '100%' : '70vh' }}>
            <Log ref={logRef} aria-live="polite" aria-label="Conversation">
                {turns.length === 0 && (
                    <div>
                        <p style={{ color: tokens.color.textMuted }}>
                            Ask about data, metrics, owners, lineage or quality. Answers only use assets you can access.
                        </p>
                        {STARTERS.map((s) => (
                            <Tag key={s} style={{ cursor: 'pointer', marginBottom: 6 }} onClick={() => void send(s)}>
                                {s}
                            </Tag>
                        ))}
                    </div>
                )}
                {turns.map((t, i) => (
                    // eslint-disable-next-line react/no-array-index-key
                    <Bubble key={i} $user={t.role === 'user'}>
                        {t.role === 'user' && t.text}
                        {t.error && <Alert type="error" message={`Assistant unavailable: ${t.error}`} />}
                        {t.role === 'assistant' && !t.error && (
                            <>
                                {(t.response?.tool_calls ?? []).length > 0 && (
                                    <div style={{ fontSize: 12, color: tokens.color.textMuted, marginBottom: 4 }}>
                                        ⚙ {(t.response?.tool_calls ?? []).map((c) => c.summary || c.name).join(' · ')}
                                    </div>
                                )}
                                <MarkdownView source={t.text} />
                                {(t.response?.citations ?? []).length > 0 && (
                                    <div aria-label="Sources">
                                        {(t.response?.citations ?? []).map((u) => (
                                            <Tag key={u} style={{ cursor: 'pointer' }} onClick={() => history.push(assetPath(u))}>
                                                {shortUrn(u)}
                                            </Tag>
                                        ))}
                                    </div>
                                )}
                                {(t.response?.suggested_actions ?? []).length > 0 && (
                                    <div style={{ marginTop: 6 }}>
                                        {(t.response?.suggested_actions ?? []).map((a) => (
                                            <Button key={`${a.kind}-${a.urn}`} size="small" style={{ marginRight: 6 }} onClick={() => void act(a)}>
                                                {a.label}
                                            </Button>
                                        ))}
                                    </div>
                                )}
                            </>
                        )}
                    </Bubble>
                ))}
                {busy && <Bubble $user={false}>Thinking…</Bubble>}
            </Log>
            <Input.Search
                aria-label="Ask the assistant"
                placeholder="Ask about data, metrics, lineage…"
                enterButton="Send"
                value={text}
                loading={busy}
                onChange={(e) => setText(e.target.value)}
                onSearch={(v) => void send(v)}
            />
        </div>
    );
}

export function AssistantSlideOver() {
    const session = useDheSession();
    return (
        <Drawer
            title="✨ Assistant"
            placement="right"
            width={460}
            open={session.assistantOpen}
            onClose={() => session.setAssistantOpen(false)}
            bodyStyle={{ display: 'flex', flexDirection: 'column' }}
        >
            {session.assistantOpen && <AssistantChat compact />}
        </Drawer>
    );
}
