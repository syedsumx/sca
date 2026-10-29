import { Button, Input, Space } from 'antd';
import React, { useState } from 'react';

import { useConsumerApi } from '../api/hooks';
import type { FeedbackIn } from '../api/types';
import { Muted } from './tokens';

/** 👍 / 👎 on an AI answer; a down vote asks what was wrong. Ratings feed the AI evaluation suite. */
export function Feedback({ kind, question, answer, context }: Omit<FeedbackIn, 'rating' | 'comment'>) {
    const { client } = useConsumerApi();
    const [state, setState] = useState<'idle' | 'why' | 'sent'>('idle');
    const [comment, setComment] = useState('');
    const send = async (rating: 'up' | 'down', note = '') => {
        try {
            await client.rateAnswer({ kind, rating, question, answer, comment: note, context });
        } finally {
            setState('sent');
        }
    };
    if (state === 'sent') return <Muted style={{ fontSize: 12 }}>Thanks for the feedback.</Muted>;
    if (state === 'why')
        return (
            <Space.Compact style={{ width: '100%', marginTop: 4 }}>
                <Input
                    size="small"
                    aria-label="What was wrong"
                    placeholder="What was wrong? (optional)"
                    value={comment}
                    onChange={(e) => setComment(e.target.value)}
                    onPressEnter={() => void send('down', comment)}
                />
                <Button size="small" onClick={() => void send('down', comment)}>
                    Send
                </Button>
            </Space.Compact>
        );
    return (
        <Space size={4} style={{ marginTop: 4 }}>
            <Button size="small" type="text" aria-label="Good answer" onClick={() => void send('up')}>
                👍
            </Button>
            <Button size="small" type="text" aria-label="Bad answer" onClick={() => setState('why')}>
                👎
            </Button>
        </Space>
    );
}
