import { List, Tag } from 'antd';
import React from 'react';
import { Link } from 'react-router-dom';

import { useDheQuery } from '../api/hooks';
import type { AccessRequest, Finding, FormTask } from '../api/types';
import { InboxList } from '../components/Notifications';
import { MyPrivacyParts } from '../components/Privacy';
import { Muted, Page, SectionTitle, Surface } from '../components/tokens';
import { PageHeader } from '../guide/PageHeader';
import { pageHelp } from '../guide/help';

interface Waiting {
    key: string;
    label: string;
    n: number | null;
    to: string;
    /** There are more than ``n`` (the server's page limit was reached). */
    more?: boolean;
}

/**
 * The header's Inbox (L149): your notifications, and everything waiting for you elsewhere (access requests to decide,
 * compliance forms, the semantics review queue, flow approvals), each linking to its one home.
 */
export function InboxPage() {
    const requests = useDheQuery<AccessRequest[]>('/marketplace/access-requests?scope=inbox', (c, signal) =>
        c.listAccessRequests({ scope: 'inbox' }, { signal }));
    const forms = useDheQuery<FormTask[]>('/governance/forms/tasks', (c, signal) => c.formTasks('PENDING', { signal }));
    // The review queue can be long: read up to the server's page limit and say "500+" past it, never a short count.
    const findings = useDheQuery<Finding[]>('/context/findings?status=OPEN&type=&limit=500', (c, signal) =>
        c.listFindings({ status: 'OPEN', limit: '500' }, { signal }));
    const approvals = useDheQuery<unknown[]>('/v1/flows/approvals', (c, signal) => c.request('GET', '/v1/flows/approvals', undefined, { signal }));
    const count = <T,>(q: { data?: T[]; error?: unknown }, keep: (x: T) => boolean = () => true) =>
        (q.error ? null : q.data ? q.data.filter(keep).length : null);
    const waiting: Waiting[] = [
        { key: 'requests', label: 'Access requests to decide', n: count(requests, (r) => r.state === 'PENDING'), to: '/requests' },
        { key: 'forms', label: 'Compliance forms to complete', n: count(forms), to: '/requests' },
        { key: 'review', label: 'Semantics review queue', n: count(findings), to: '/semantics?tab=review', more: (findings.data?.length ?? 0) >= 500 },
        { key: 'flows', label: 'Flow steps waiting for your approval', n: count(approvals), to: '/flows' },
    ];
    return (
        <Page>
            <PageHeader title="Inbox" help={pageHelp('/inbox')}
                description="Your notifications, and everything waiting for you: requests to decide, forms, reviews and approvals." />
            <Surface as="section" aria-labelledby="inbox-waiting" style={{ padding: 16, marginBottom: 16 }}>
                <SectionTitle id="inbox-waiting" style={{ marginTop: 0 }}>Waiting for you</SectionTitle>
                <List
                    size="small"
                    dataSource={waiting}
                    renderItem={(w) => (
                        <List.Item extra={w.n === null ? <Muted>—</Muted> : <Tag color={w.n ? 'blue' : 'default'}>{w.n}{w.more ? '+' : ''}</Tag>}>
                            <Link to={w.to}>{w.label}</Link>
                        </List.Item>
                    )}
                />
                <Muted>Infrastructure-as-code change requests to approve are in <Link to="/settings?tab=iac">Settings › Infrastructure as code</Link>.</Muted>
            </Surface>
            <section aria-labelledby="inbox-notifications">
                <SectionTitle id="inbox-notifications">Notifications</SectionTitle>
                <MyPrivacyParts />
                <InboxList />
            </section>
        </Page>
    );
}
