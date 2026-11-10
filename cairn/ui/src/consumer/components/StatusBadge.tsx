import React from 'react';
import styled from 'styled-components';

import { MessageKey, useT } from '../i18n';
import { tokens } from './tokens';

/** How a status reads: each tone has its own colour pair and its own shape, so it never depends on colour alone. */
export type StatusTone = 'ok' | 'warn' | 'error' | 'running' | 'waiting';

const OK = ['ok', 'done', 'success', 'succeeded', 'healthy', 'live', 'ready', 'applied', 'active', 'shipped', 'up', 'pass',
    'passed', 'approved', 'fresh', 'enabled'];
const ERROR = ['failed', 'failing', 'error', 'down', 'refused', 'rejected', 'cancelled', 'canceled', 'deactivated', 'fail',
    'denied', 'stale', 'critical', 'upstream_failed'];
const WARN = ['warn', 'warning', 'degraded', 'sla_missed', 'anomalous', 'partial', 'at_risk', 'retrying', 'up_for_retry'];
const RUNNING = ['running', 'started', 'in_progress', 'deploying', 'promoting', 'restoring'];

/** The tone of a state as the APIs name it (success, failed, healthy, sla_missed, …); anything else is waiting. */
export function toneOf(state: string | null | undefined): StatusTone {
    const s = (state ?? '').toLowerCase();
    if (OK.includes(s)) return 'ok';
    if (ERROR.includes(s)) return 'error';
    if (WARN.includes(s)) return 'warn';
    if (RUNNING.includes(s)) return 'running';
    return 'waiting';
}

/** A state in words: the translated label when there is one (`status.<state>`), else the state made readable. */
export function useStatusLabel(): (state: string) => string {
    const t = useT();
    return (state: string) => {
        const key = `status.${state.toLowerCase()}` as MessageKey;
        const text = t(key);
        if (text !== key) return text;
        const plain = state.replace(/[_-]+/g, ' ').trim();
        return plain ? plain[0].toUpperCase() + plain.slice(1) : state;
    };
}

const Badge = styled.span<{ $tone: StatusTone; $plain: boolean }>`
    display: inline-flex;
    align-items: center;
    gap: 6px;
    max-width: 100%;
    font-size: 12px;
    font-weight: 600;
    line-height: 1.35;
    white-space: nowrap;
    border-radius: 999px;
    padding: ${(p) => (p.$plain ? '0' : '3px 9px')};
    color: var(--cairn-${(p) => p.$tone}-fg, ${(p) => tokens.status[p.$tone].fg});
    background: ${(p) => (p.$plain ? 'transparent' : `var(--cairn-${p.$tone}-bg, ${tokens.status[p.$tone].bg})`)};
    .mark {
        flex: none;
        width: 9px;
        height: 9px;
        box-sizing: border-box;
        border: 2px solid var(--cairn-${(p) => p.$tone}-mark, ${(p) => tokens.status[p.$tone].mark});
        background: var(--cairn-${(p) => p.$tone}-mark, ${(p) => tokens.status[p.$tone].mark});
    }
    /* Shapes: ok a filled circle, running a ring, waiting a hollow circle, warn a triangle, error a square. */
    &[data-tone='ok'] .mark {
        border-radius: 50%;
    }
    &[data-tone='running'] .mark {
        border-radius: 50%;
        border-width: 3px;
        background: transparent;
    }
    &[data-tone='waiting'] .mark {
        border-radius: 50%;
        background: transparent;
    }
    &[data-tone='warn'] .mark {
        border: 0;
        width: 10px;
        clip-path: polygon(50% 0, 100% 100%, 0 100%);
    }
    &[data-tone='error'] .mark {
        border-radius: 1px;
    }
    .label {
        overflow: hidden;
        text-overflow: ellipsis;
    }
`;

export interface StatusBadgeProps {
    /** The state as the API names it; picks the tone and, without `label`, the words. */
    state?: string;
    /** Overrides the tone `state` implies. */
    tone?: StatusTone;
    /** The words to show (already translated); default: the state's label. */
    label?: React.ReactNode;
    /** Just the mark and the words, no pill (in dense lists and on cards). */
    plain?: boolean;
    title?: string;
    className?: string;
    style?: React.CSSProperties;
}

/** A status, readable without colour: a shape, the words, and a colour pair that meets contrast. */
export function StatusBadge({ state, tone, label, plain = false, title, className, style }: StatusBadgeProps) {
    const words = useStatusLabel();
    const tn = tone ?? toneOf(state);
    return (
        <Badge className={`cairn-status${className ? ` ${className}` : ''}`} data-tone={tn} data-state={state} $tone={tn}
            $plain={plain} title={title} style={style}>
            <span className="mark" aria-hidden="true" />
            <span className="label">{label ?? (state ? words(state) : '')}</span>
        </Badge>
    );
}
