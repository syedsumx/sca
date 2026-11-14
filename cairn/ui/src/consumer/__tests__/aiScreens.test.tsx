import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { AgentExamples, ConnectIde, GatewayModels, McpServers, servedByLabel } from '../components/AiScreens';
import { CairnProvider } from '../context/CairnProvider';
import EXAMPLES from '../resources/agentExamples.generated.json';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

function wrap(node: React.ReactNode, routes: Record<string, unknown>, calls: MockCall[] = []) {
    const { client } = makeClient({ 'GET /ui/config': UI_CONFIG, ...routes }, calls);
    return render(<MemoryRouter><CairnProvider client={client} cache={new QueryCache()}>{node}</CairnProvider></MemoryRouter>);
}

it('says where each model runs', async () => {
    expect(servedByLabel({ id: 'x', served_by: { upstream: 'b', kind: 'bedrock', region: 'us-east-1', model_id: 'm' } }))
        .toBe('Amazon Bedrock · us-east-1');
    expect(servedByLabel({ id: 'r', cairn_route: true, candidates: ['a', 'b'] })).toBe('Route over 2 models');
    wrap(<GatewayModels />, { 'GET /v1/ai/gateway/models': { data: [
        { id: 'medium', object: 'model', served_by: { upstream: 'foundry', kind: 'foundry', region: null, model_id: 'medium-deployment' } },
        { id: 'large-eu', object: 'model', served_by: { upstream: 'vertex', kind: 'vertex', region: 'europe-west4', model_id: 'example-large' } },
    ] } });
    expect(await screen.findByText('Azure AI Foundry')).toBeTruthy();
    expect(screen.getByText('Google Vertex AI · europe-west4')).toBeTruthy();
    expect(screen.getByText('medium-deployment')).toBeTruthy();
});

it('registers an MCP server with its credential as a secret scope reference', async () => {
    const calls: MockCall[] = [];
    let servers: unknown[] = [];
    wrap(<McpServers />, {
        'GET /v1/ai/mcp-servers': () => ({ servers }),
        'PUT /v1/ai/mcp-servers/tracker': (b: Record<string, unknown>) => { servers = [{ name: 'tracker', ...b }]; return servers[0]; },
    }, calls);
    fireEvent.click(await screen.findByRole('button', { name: 'Register a server' }));
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'tracker' } });
    fireEvent.change(screen.getByLabelText('URL'), { target: { value: 'https://mcp.tracker.example/mcp' } });
    fireEvent.change(screen.getByLabelText('Deny tools (comma-separated, * allowed)'), { target: { value: 'delete_*, drop_*' } });
    fireEvent.click(screen.getByRole('button', { name: 'Register' }));
    await waitFor(() => expect(calls.find((c) => c.method === 'PUT')).toBeTruthy());
    const body = calls.find((c) => c.method === 'PUT')?.body as Record<string, unknown>;
    expect(body).toMatchObject({ url: 'https://mcp.tracker.example/mcp', auth: { type: 'none' }, deny_tools: ['delete_*', 'drop_*'] });
    expect(await screen.findByText('deny delete_*, drop_*')).toBeTruthy();
});

it('shows the configuration cairn ide init writes for the chosen editor', async () => {
    const calls: MockCall[] = [];
    wrap(<ConnectIde />, { 'GET /v1/ai/ide-config': () => ({
        client: 'vscode', url: 'https://c.example/cairn/mcp/mcp?toolsets=catalog', file: '.vscode/mcp.json',
        content: '{"servers": {"cairn": {}}}\n', command: 'cairn ide init --url https://c.example --client vscode',
        secret: null, clients: ['vscode', 'copilot-agent'], toolsets: ['catalog', 'iac'], default_toolsets: ['catalog'] }) }, calls);
    expect(await screen.findByLabelText('.vscode/mcp.json')).toBeTruthy();
    expect(screen.getByLabelText('Or run').textContent).toContain('cairn ide init');
    expect(calls[0].url.searchParams.get('client')).toBe('vscode');
});

it('installs an example agent as a custom agent', async () => {
    const calls: MockCall[] = [];
    let agents: { name: string }[] = [];
    const first = (EXAMPLES as { agent: { name: string } }[])[0].agent.name;
    wrap(<AgentExamples />, {
        'GET /v1/custom-agents': () => agents,
        'POST /v1/custom-agents': (b: { name: string }) => { agents = [b]; return b; },
    }, calls);
    fireEvent.click(await screen.findByRole('button', { name: `Install ${first}` }));
    await waitFor(() => expect(calls.find((c) => c.method === 'POST')?.body).toMatchObject({ name: first }));
    expect(await screen.findByText('installed')).toBeTruthy();
});
