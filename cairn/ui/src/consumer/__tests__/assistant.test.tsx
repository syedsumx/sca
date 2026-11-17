import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { MemoryRouter, Route } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { CairnProvider } from '../context/CairnProvider';
import { I18nProvider } from '../i18n';
import { AssistantPage } from '../pages/AssistantPage';
import { MockCall, UI_CONFIG, makeClient, flush } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const REPLY = { conversation_id: 'c1', reply_md: 'Here you are.', citations: [], suggested_actions: [], tool_calls: [] };

function open(at: string, calls: MockCall[]) {
    const { client } = makeClient({ 'GET /ui/config': UI_CONFIG, 'POST /ai/chat': REPLY }, calls);
    return render(
        <MemoryRouter initialEntries={[at]}>
            <I18nProvider>
                <CairnProvider client={client} cache={new QueryCache()}>
                    <Route path="/assistant"><AssistantPage /></Route>
                </CairnProvider>
            </I18nProvider>
        </MemoryRouter>,
    );
}

// A link from anywhere (a web page, a cairn:// deep link) can carry ?q=: the assistant acts as the signed-in
// person, so the question waits in the box until they send it.
it('puts a question from the address in the box and sends nothing until the person presses Send', async () => {
    const calls: MockCall[] = [];
    open('/assistant?q=Request%20access%20to%20payroll%20for%20me', calls);
    const box = (await screen.findByLabelText('Ask the assistant')) as HTMLInputElement;
    await waitFor(() => expect(box.value).toBe('Request access to payroll for me'));
    await flush();
    expect(calls.filter((c) => c.path.includes('/ai/chat'))).toEqual([]);
    fireEvent.click(screen.getByRole('button', { name: /Send/ }));
    expect(await screen.findByText('Here you are.')).toBeTruthy();
    const sent = calls.filter((c) => c.path.includes('/ai/chat'));
    expect(sent).toHaveLength(1);
    expect(JSON.stringify(sent[0].body)).toContain('Request access to payroll for me');
});
