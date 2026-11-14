import { Alert, Button, Checkbox, Modal, Progress as Bar, Radio, Space, Tag } from 'antd';
import React, { useEffect, useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import styled from 'styled-components';

import { Muted, Page, Surface, tokens } from '../components/tokens';
import { MessageKey, useT } from '../i18n';
import { useGuide } from './GuideProvider';
import { ROLES, Role, progress } from './onboarding';
import { PageHeader } from './PageHeader';

const ROLE_KEYS: Record<Role, { label: MessageKey; hint: MessageKey }> = {
    analyst: { label: 'guide.role.analyst', hint: 'guide.role.analystHint' },
    engineer: { label: 'guide.role.engineer', hint: 'guide.role.engineerHint' },
    steward: { label: 'guide.role.steward', hint: 'guide.role.stewardHint' },
    admin: { label: 'guide.role.admin', hint: 'guide.role.adminHint' },
    explorer: { label: 'guide.role.explorer', hint: 'guide.role.explorerHint' },
};

const RoleList = styled(Radio.Group)`
    && {
        display: grid;
        gap: 8px;
        width: 100%;
    }
    .ant-radio-wrapper {
        display: flex;
        align-items: flex-start;
        margin: 0;
        padding: 10px 12px;
        border: 1px solid ${tokens.color.border};
        border-radius: ${tokens.radius.sm};
    }
    .ant-radio-wrapper-checked {
        border-color: ${tokens.color.primary};
        background: ${tokens.color.primarySoft};
    }
    .ant-radio-wrapper:focus-within {
        outline: 2px solid ${tokens.color.focus};
        outline-offset: 2px;
    }
`;

/** The role choice: radio cards, the profile's suggestion marked. */
export function RolePicker({ value, onChange, suggested }: { value: Role | null; onChange: (r: Role) => void; suggested: Role | null }) {
    const t = useT();
    return (
        <RoleList value={value ?? undefined} onChange={(e) => onChange(e.target.value as Role)} aria-label={t('guide.role')}>
            {ROLES.map((r) => (
                <Radio key={r} value={r}>
                    <strong>{t(ROLE_KEYS[r].label)}</strong>
                    {suggested === r && <Tag style={{ marginLeft: 8 }}>{t('guide.role.suggested')}</Tag>}
                    <br />
                    <Muted>{t(ROLE_KEYS[r].hint)}</Muted>
                </Radio>
            ))}
        </RoleList>
    );
}

/** First sign-in: what the person mostly does here (asked once; "Not now" is an answer too). Asked on the home page
 * only: a deep link (a bookmark, a notification) opens what it points at without a question in the way. */
export function RoleDialog() {
    const g = useGuide();
    const t = useT();
    const { pathname } = useLocation();
    const home = pathname === '/discover' || pathname === '/';
    const open = home && g.enabled && g.available && !g.ui.onboarding.asked && !g.ui.onboarding.role;
    const [role, setRole] = useState<Role | null>(null);
    useEffect(() => {
        if (open && role === null && g.suggestedRole) setRole(g.suggestedRole);
    }, [open, role, g.suggestedRole]);
    if (!open) return null;
    return (
        <Modal
            open
            title={t('guide.role.title')}
            onCancel={() => void g.save({ onboarding: { asked: true } })}
            footer={
                <Space>
                    <Button onClick={() => void g.save({ onboarding: { asked: true } })}>{t('guide.role.skip')}</Button>
                    <Button type="primary" disabled={!role} onClick={() => void g.save({ onboarding: { role, asked: true, dismissed: false } })}>
                        {t('guide.role.continue')}
                    </Button>
                </Space>
            }
        >
            <p><Muted>{t('guide.role.intro')}</Muted></p>
            <RolePicker value={role} onChange={setRole} suggested={g.suggestedRole} />
        </Modal>
    );
}

const Steps = styled.ol`
    list-style: none;
    margin: 0;
    padding: 0;
    li {
        display: flex;
        gap: 12px;
        align-items: flex-start;
        padding: 14px 4px;
        border-bottom: 1px solid ${tokens.color.border};
    }
    li:last-child {
        border-bottom: 0;
    }
    .body {
        flex: 1;
        min-width: 0;
    }
    .body a {
        font-weight: 600;
        font-size: 15px;
    }
    .done a {
        color: ${tokens.color.textMuted};
        text-decoration: line-through;
    }
`;

/** "Get started": the role's checklist, each step linking to where it is done; steps tick themselves. */
export function GetStartedPage() {
    const g = useGuide();
    const t = useT();
    const [changing, setChanging] = useState(false);
    const [checking, setChecking] = useState(false);
    const { role, done, dismissed } = g.ui.onboarding;
    const p = progress(role, done);
    useEffect(() => {
        if (g.enabled && role) void g.detect();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [g.enabled, role]);
    const check = async () => {
        setChecking(true);
        try {
            await g.detect();
        } finally {
            setChecking(false);
        }
    };
    return (
        <Page>
            <PageHeader title={t('guide.title')} description={t('guide.intro')}
                actions={g.enabled && role ? (
                    <Space wrap>
                        <Button onClick={() => setChanging(!changing)} aria-expanded={changing}>{t('guide.changeRole')}</Button>
                        <Button onClick={() => void g.save({ onboarding: { dismissed: !dismissed } })}>
                            {t(dismissed ? 'guide.show' : 'guide.hide')}
                        </Button>
                    </Space>
                ) : undefined} />
            {!g.available ? null : !g.enabled ? (
                <Alert type="info" showIcon message={t('guide.off')} />
            ) : !role || changing ? (
                <Surface style={{ padding: 20, maxWidth: 640 }}>
                    <p>{t('guide.chooseRole')}</p>
                    <RolePicker value={role} suggested={g.suggestedRole}
                        onChange={(r) => {
                            setChanging(false);
                            void g.save({ onboarding: { role: r, asked: true, dismissed: false } });
                        }} />
                </Surface>
            ) : dismissed ? (
                <Alert type="info" showIcon message={t('guide.hidden')} />
            ) : (
                <Surface as="section" aria-labelledby="guide-checklist" style={{ padding: 20 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 16, flexWrap: 'wrap', marginBottom: 8 }}>
                        <h2 id="guide-checklist" style={{ margin: 0, fontSize: 18 }}>
                            {t('guide.role')}: {t(ROLE_KEYS[role].label)}
                        </h2>
                        <span style={{ flex: 1 }} />
                        <span aria-live="polite">{t('guide.progress', { done: p.done, total: p.total })}</span>
                        <Button size="small" onClick={() => void check()} loading={checking}>{t('guide.check')}</Button>
                    </div>
                    <Bar percent={Math.round((100 * p.done) / Math.max(1, p.total))} showInfo={false}
                        aria-label={t('guide.progress', { done: p.done, total: p.total })} />
                    {p.complete && <Alert type="success" showIcon message={t('guide.complete')} style={{ margin: '12px 0' }} />}
                    <Steps>
                        {p.steps.map((s) => (
                            <li key={s.id} className={s.done ? 'done' : undefined} data-step={s.id} data-done={s.done ? 'true' : 'false'}>
                                <Checkbox checked={s.done} onChange={() => g.toggleDone(s.id)}
                                    aria-label={t(s.done ? 'guide.markNotDone' : 'guide.markDone', { step: t(s.title) })} />
                                <div className="body">
                                    <Link to={s.to}>{t(s.title)}</Link>
                                    <div><Muted>{t(s.hint)}</Muted></div>
                                </div>
                                <Tag color={s.done ? 'green' : undefined}>{t(s.done ? 'guide.stepDone' : 'guide.stepOpen')}</Tag>
                            </li>
                        ))}
                    </Steps>
                </Surface>
            )}
        </Page>
    );
}
