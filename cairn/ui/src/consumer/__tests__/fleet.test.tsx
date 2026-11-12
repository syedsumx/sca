import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import React from 'react';

import { demoFleet } from '../../../fleet/demoFleet';
import { FleetConsole, SignIn, httpApi } from '../../../fleet/FleetConsole';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

it('lists installs worst first, with what needs attention', async () => {
    render(<FleetConsole api={demoFleet} />);
    await screen.findByText('Fabrikam Energy');
    const first = document.querySelector('tr[data-row-key]') as HTMLElement;   // the first install (not the header)
    expect(within(first).getByText('Fabrikam Energy')).toBeTruthy();
    expect(within(first).getByText('silent')).toBeTruthy();
    const table = within(document.querySelector('.ant-table-tbody') as HTMLElement);
    for (const tag of ['security update', 'update failed', 'over seats', 'unhealthy', 'licence expiring'])
        expect(table.getByText(tag)).toBeTruthy();
    expect(table.getAllByText('nothing').length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText('Nothing to do')).toBeTruthy();                  // the summary tiles
});

it('registers an install and shows its key once', async () => {
    render(<FleetConsole api={demoFleet} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Register an install' }));
    fireEvent.change(screen.getByLabelText('Customer'), { target: { value: 'Initech' } });
    fireEvent.change(screen.getByLabelText('Install name'), { target: { value: 'initech-prod' } });
    fireEvent.click(screen.getByRole('button', { name: 'Register' }));
    const key = (await screen.findByLabelText('Check-in key')) as HTMLTextAreaElement;
    expect(key.value).toMatch(/^[0-9a-f]{64}$/);
    await waitFor(() => expect(screen.getByText('initech-prod')).toBeTruthy());
});

it('signs in with an operator token and sends it to the console', async () => {
    const onToken = vi.fn();
    render(<SignIn onToken={onToken} />);
    fireEvent.change(screen.getByLabelText('Operator token'), { target: { value: '  cfo_abc  ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(onToken).toHaveBeenCalledWith('cfo_abc');
    const seen: RequestInit[] = [];
    const fetchMock = vi.fn(async (_u: string, init: RequestInit) => {
        seen.push(init);
        return new Response(JSON.stringify({ detail: 'sign in with a fleet operator token' }), { status: 401 });
    });
    vi.stubGlobal('fetch', fetchMock);
    await expect(httpApi('', 'cfo_abc').installs()).rejects.toThrow('sign in with a fleet operator token');
    expect((seen[0].headers as Record<string, string>).Authorization).toBe('Bearer cfo_abc');
    vi.unstubAllGlobals();
});
