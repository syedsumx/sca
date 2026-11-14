import styled from 'styled-components';

import { tokens } from '../components/tokens';

/**
 * The Flows workspace's look, in CSS variables from the design tokens: light by default, dark with
 * `data-theme="dark"` (or the system's preference with `data-theme="auto"`).
 */
const light = `
    --fl-bg: ${tokens.color.surfaceAlt};
    --fl-canvas: ${tokens.color.surfaceAlt};
    --fl-dot: ${tokens.color.borderStrong};
    --fl-surface: ${tokens.color.surface};
    --fl-border: ${tokens.color.border};
    --fl-text: ${tokens.color.text};
    --fl-muted: ${tokens.color.textMuted};
    --fl-primary: ${tokens.color.primary};
    --fl-primary-soft: ${tokens.color.primarySoft};
    --fl-good: ${tokens.color.good};
    --fl-warn: ${tokens.color.warn};
    --fl-bad: ${tokens.color.bad};
    --fl-run: ${tokens.status.running.mark};
    --fl-ok-mark: ${tokens.status.ok.mark};
    --fl-waiting: ${tokens.color.textMuted};
    --fl-waiting-mark: ${tokens.color.waiting};
    --fl-edge: ${tokens.color.waiting};
    --fl-shadow: 0 1px 2px rgba(15,23,42,.06);
    --fl-focus: ${tokens.color.focus};
`;
const dark = `
    --fl-bg: #0B1120;
    --fl-canvas: #0F172A;
    --fl-dot: #263244;
    --fl-surface: #111827;
    --fl-border: #263244;
    --fl-text: #F1F5F9;
    --fl-muted: #A8B3C4;
    --fl-primary: #93B4FF;
    --fl-primary-soft: #172554;
    --fl-good: #4ADE80;
    --fl-warn: #FBBF24;
    --fl-bad: #F87171;
    --fl-run: #60A5FA;
    --fl-ok-mark: #4ADE80;
    --fl-waiting: #A8B3C4;
    --fl-waiting-mark: #94A3B8;
    --fl-edge: #64748B;
    --fl-shadow: 0 1px 2px rgba(0,0,0,.4), 0 6px 18px rgba(0,0,0,.35);
    --fl-focus: #93B4FF;
`;

export const FlowsRoot = styled.div`
    ${light}
    &[data-theme='dark'] {
        ${dark}
    }
    @media (prefers-color-scheme: dark) {
        &[data-theme='auto'] {
            ${dark}
        }
    }
    background: var(--fl-bg);
    color: var(--fl-text);
    font-family: ${tokens.font};
    min-height: calc(100vh - 64px);
    display: flex;
    flex-direction: column;

    .fl-top {
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 12px 20px;
        border-bottom: 1px solid var(--fl-border);
        background: var(--fl-surface);
        flex-wrap: wrap;
    }
    .fl-title {
        font-size: 18px;
        font-weight: 650;
        margin: 0;
        color: var(--fl-text);
    }
    .fl-muted {
        color: var(--fl-muted);
        font-size: 12px;
    }
    .fl-btn {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        border: 1px solid var(--fl-border);
        background: var(--fl-surface);
        color: var(--fl-text);
        border-radius: 8px;
        padding: 5px 10px;
        font-size: 13px;
        cursor: pointer;
        line-height: 20px;
    }
    .fl-btn:hover {
        border-color: var(--fl-primary);
    }
    .fl-btn:disabled {
        opacity: 0.5;
        cursor: not-allowed;
    }
    .fl-btn.primary {
        background: var(--fl-primary);
        border-color: var(--fl-primary);
        color: #fff;
    }
    .fl-btn.danger {
        color: var(--fl-bad);
    }
    .fl-btn:focus-visible,
    .fl-node:focus-visible,
    .fl-pal-item:focus-visible,
    .fl-viewport:focus-visible,
    .fl-tab:focus-visible,
    .fl-list-item:focus-visible {
        outline: 2px solid var(--fl-focus);
        outline-offset: 2px;
    }
    .fl-tabs {
        display: flex;
        gap: 4px;
        margin-left: 8px;
    }
    .fl-tab {
        border: 0;
        background: transparent;
        color: var(--fl-muted);
        padding: 6px 10px;
        border-radius: 8px;
        cursor: pointer;
        font-size: 13px;
    }
    .fl-tab[aria-selected='true'] {
        background: var(--fl-primary-soft);
        color: var(--fl-text);
        font-weight: 600;
    }
    .fl-body {
        flex: none;
        display: grid;
        grid-template-columns: 220px minmax(0, 1fr) 340px;
        height: max(520px, calc(100vh - 250px));
    }
    @media (max-width: 1100px) {
        .fl-body {
            grid-template-columns: 180px minmax(0, 1fr);
            grid-template-rows: max(480px, 70vh) auto;
            height: auto;
        }
        .fl-side {
            grid-column: 1 / -1;
            height: auto;
        }
    }
    .fl-palette,
    .fl-side {
        background: var(--fl-surface);
        border-right: 1px solid var(--fl-border);
        padding: 12px;
        overflow: auto;
        height: 100%;
        box-sizing: border-box;
    }
    .fl-side {
        border-right: 0;
        border-left: 1px solid var(--fl-border);
    }
    .fl-group {
        margin: 10px 0 4px;
        font-size: 11px;
        letter-spacing: 0.06em;
        text-transform: uppercase;
        color: var(--fl-muted);
        font-weight: 650;
    }
    .fl-pal-item {
        display: flex;
        align-items: center;
        gap: 8px;
        width: 100%;
        text-align: left;
        border: 1px solid var(--fl-border);
        background: var(--fl-surface);
        color: var(--fl-text);
        border-radius: 8px;
        padding: 6px 8px;
        margin: 4px 0;
        cursor: grab;
        font-size: 13px;
    }
    .fl-pal-item:hover {
        box-shadow: var(--fl-shadow);
    }
    .fl-chip {
        width: 26px;
        height: 26px;
        border-radius: 7px;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        color: #fff;
        flex: none;
    }
    .fl-viewport {
        position: relative;
        overflow: hidden;
        background-color: var(--fl-canvas);
        background-image: radial-gradient(var(--fl-dot) 1px, transparent 1px);
        cursor: grab;
        height: 100%;
        min-height: 420px;
        touch-action: none;
    }
    .fl-viewport.panning {
        cursor: grabbing;
    }
    .fl-world {
        position: absolute;
        left: 0;
        top: 0;
        transform-origin: 0 0;
    }
    .fl-edges {
        position: absolute;
        left: 0;
        top: 0;
        overflow: visible;
        pointer-events: none;
    }
    .fl-edge {
        fill: none;
        stroke: var(--fl-edge);
        stroke-width: 2;
    }
    .fl-edge-hit {
        fill: none;
        stroke: transparent;
        stroke-width: 14;
        pointer-events: stroke;
        cursor: pointer;
    }
    .fl-edge.selected {
        stroke: var(--fl-primary);
        stroke-width: 3;
    }
    .fl-edge.live {
        stroke: var(--fl-primary);
        stroke-dasharray: 8 6;
        animation: fl-dash 0.8s linear infinite;
    }
    .fl-edge.ok {
        stroke: var(--fl-good);
    }
    .fl-edge.temp {
        stroke: var(--fl-primary);
        stroke-dasharray: 4 4;
    }
    @keyframes fl-dash {
        to {
            stroke-dashoffset: -28;
        }
    }
    @media (prefers-reduced-motion: reduce) {
        .fl-edge.live {
            animation: none;
        }
        .fl-node.st-running {
            animation: none;
        }
    }
    .fl-node {
        position: absolute;
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 10px 12px;
        border-radius: 12px;
        background: var(--fl-surface);
        border: 1.5px solid var(--fl-border);
        box-shadow: var(--fl-shadow);
        cursor: pointer;
        user-select: none;
        color: var(--fl-text);
    }
    /* Selection is an outline outside the card, so it never reads as a state (states colour the border). */
    .fl-node.selected {
        outline: 3px solid var(--fl-primary);
        outline-offset: 3px;
    }
    .fl-node.has-error {
        border-color: var(--fl-bad);
    }
    .fl-node .fl-name {
        font-weight: 600;
        font-size: 13px;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .fl-node .fl-sub {
        font-size: 11px;
        color: var(--fl-muted);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    /* A step's state: a mark with its own shape and the word (Done, Running, Waiting…), never colour alone. */
    .fl-status {
        display: inline-flex;
        align-items: center;
        gap: 5px;
        font-size: 11px;
        font-weight: 600;
        line-height: 1.4;
        white-space: nowrap;
        color: var(--fl-waiting);
    }
    .fl-node .fl-status {
        position: absolute;
        top: -10px;
        right: 10px;
        padding: 1px 8px;
        border-radius: 999px;
        background: var(--fl-surface);
        border: 1px solid var(--fl-border);
    }
    .fl-status .fl-dot {
        flex: none;
        width: 8px;
        height: 8px;
        box-sizing: border-box;
        border-radius: 50%;
        border: 2px solid var(--fl-waiting-mark);
        background: transparent;
    }
    .fl-status.ok,
    .fl-status.succeeded {
        color: var(--fl-good);
    }
    .fl-status.ok .fl-dot,
    .fl-status.succeeded .fl-dot {
        border-color: var(--fl-ok-mark);
        background: var(--fl-ok-mark);
    }
    .fl-status.failed,
    .fl-status.cancelled {
        color: var(--fl-bad);
    }
    .fl-status.failed .fl-dot,
    .fl-status.cancelled .fl-dot {
        border-radius: 1px;
        border-color: var(--fl-bad);
        background: var(--fl-bad);
    }
    .fl-status.running,
    .fl-status.retrying,
    .fl-status.queued {
        color: var(--fl-primary);
    }
    .fl-status.running .fl-dot,
    .fl-status.retrying .fl-dot,
    .fl-status.queued .fl-dot {
        border-width: 3px;
        border-color: var(--fl-run);
    }
    .fl-status.waiting {
        color: var(--fl-warn);
    }
    .fl-status.waiting .fl-dot {
        border: 0;
        border-radius: 0;
        width: 9px;
        background: var(--fl-warn);
        clip-path: polygon(50% 0, 100% 100%, 0 100%);
    }
    .fl-status.skipped .fl-dot {
        border-style: dashed;
    }
    .fl-node.st-ok {
        border-color: var(--fl-ok-mark);
    }
    .fl-node.st-failed,
    .fl-node.st-cancelled {
        border-color: var(--fl-bad);
    }
    .fl-node.st-running,
    .fl-node.st-retrying {
        border: 2px solid var(--fl-run);
        animation: fl-pulse 1.4s ease-in-out infinite;
    }
    .fl-node.st-waiting {
        border-color: var(--fl-warn);
    }
    .fl-node.st-skipped,
    .fl-node.st-pending {
        border-style: dashed;
        border-color: var(--fl-waiting-mark);
    }
    .fl-legend {
        position: absolute;
        top: 10px;
        left: 12px;
        display: flex;
        flex-wrap: wrap;
        gap: 6px 14px;
        margin: 0;
        padding: 6px 10px;
        list-style: none;
        background: var(--fl-surface);
        border: 1px solid var(--fl-border);
        border-radius: 10px;
        box-shadow: var(--fl-shadow);
    }
    .fl-runner {
        display: grid;
        gap: 8px;
        border: 0;
        margin: 0 0 14px;
        padding: 0;
    }
    .fl-runner legend {
        font-size: 14px;
        font-weight: 600;
        margin-bottom: 6px;
        color: var(--fl-text);
    }
    .fl-runner label {
        display: flex;
        gap: 10px;
        align-items: flex-start;
        min-height: 44px;
        padding: 10px 12px;
        border: 1px solid var(--fl-border);
        border-radius: 10px;
        font-size: 13px;
        cursor: pointer;
        color: var(--fl-text);
    }
    .fl-runner label.on {
        border: 2px solid var(--fl-primary);
        background: var(--fl-primary-soft);
    }
    .fl-runner input {
        margin-top: 3px;
        accent-color: var(--fl-primary);
    }
    .fl-runner span span {
        display: block;
        color: var(--fl-muted);
        font-size: 12px;
    }
    @keyframes fl-pulse {
        50% {
            box-shadow: 0 0 0 5px var(--fl-primary-soft), var(--fl-shadow);
        }
    }
    .fl-handle {
        position: absolute;
        width: 14px;
        height: 14px;
        border-radius: 50%;
        background: var(--fl-surface);
        border: 2px solid var(--fl-edge);
        top: calc(50% - 7px);
    }
    .fl-handle.in {
        left: -8px;
    }
    .fl-handle.out {
        right: -8px;
        cursor: crosshair;
    }
    .fl-handle.out:hover {
        border-color: var(--fl-primary);
        background: var(--fl-primary);
    }
    .fl-band {
        position: absolute;
        border: 1px dashed var(--fl-primary);
        background: rgba(81, 69, 229, 0.08);
        pointer-events: none;
    }
    .fl-controls {
        position: absolute;
        left: 12px;
        bottom: 12px;
        display: flex;
        flex-direction: column;
        gap: 4px;
        background: var(--fl-surface);
        border: 1px solid var(--fl-border);
        border-radius: 10px;
        padding: 4px;
        box-shadow: var(--fl-shadow);
    }
    .fl-controls .fl-btn {
        border: 0;
        padding: 4px;
    }
    .fl-minimap {
        position: absolute;
        right: 12px;
        bottom: 12px;
        background: var(--fl-surface);
        border: 1px solid var(--fl-border);
        border-radius: 10px;
        box-shadow: var(--fl-shadow);
        cursor: pointer;
    }
    .fl-minimap .vp {
        fill: var(--fl-primary-soft);
        fill-opacity: 0.5;
        stroke: var(--fl-primary);
    }
    .fl-hint {
        position: absolute;
        top: 10px;
        left: 50%;
        transform: translateX(-50%);
        background: var(--fl-surface);
        border: 1px solid var(--fl-border);
        border-radius: 999px;
        padding: 4px 12px;
        font-size: 12px;
        color: var(--fl-muted);
        box-shadow: var(--fl-shadow);
    }
    .fl-field {
        display: block;
        margin: 10px 0;
    }
    .fl-field > span {
        display: block;
        font-size: 12px;
        font-weight: 600;
        margin-bottom: 4px;
        color: var(--fl-text);
    }
    .fl-field input,
    .fl-field select,
    .fl-field textarea,
    .fl-code {
        width: 100%;
        border: 1px solid var(--fl-border);
        background: var(--fl-canvas);
        color: var(--fl-text);
        border-radius: 8px;
        padding: 6px 8px;
        font-size: 13px;
        font-family: inherit;
    }
    .fl-field input[type='checkbox'],
    .fl-checks input {
        width: auto;
        margin: 0 4px 0 0;
        vertical-align: middle;
    }
    .fl-checks label {
        display: inline-flex;
        align-items: center;
    }
    .fl-field textarea,
    .fl-code {
        font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        font-size: 12.5px;
    }
    .fl-field .fl-err,
    .fl-err {
        color: var(--fl-bad);
        font-size: 12px;
        margin-top: 3px;
    }
    .fl-field input[aria-invalid='true'],
    .fl-field textarea[aria-invalid='true'] {
        border-color: var(--fl-bad);
    }
    .fl-checks {
        display: flex;
        flex-wrap: wrap;
        gap: 4px 10px;
        font-size: 13px;
    }
    .fl-code-wrap {
        padding: 16px 20px;
        display: grid;
        grid-template-columns: minmax(0, 1fr) 320px;
        gap: 16px;
    }
    .fl-code {
        min-height: 560px;
        line-height: 1.5;
        tab-size: 2;
    }
    .fl-pane {
        padding: 16px 20px;
        max-width: 1100px;
    }
    .fl-card {
        background: var(--fl-surface);
        border: 1px solid var(--fl-border);
        border-radius: 12px;
        padding: 14px;
        box-shadow: var(--fl-shadow);
    }
    .fl-list {
        list-style: none;
        margin: 0;
        padding: 0;
    }
    .fl-list-item {
        display: flex;
        align-items: center;
        gap: 8px;
        width: 100%;
        border: 0;
        background: transparent;
        color: var(--fl-text);
        text-align: left;
        padding: 6px 4px;
        border-radius: 6px;
        cursor: pointer;
        font-size: 13px;
    }
    .fl-list-item:hover,
    .fl-list-item[aria-current='true'] {
        background: var(--fl-primary-soft);
    }
    .fl-badge {
        display: inline-flex;
        align-items: center;
        gap: 5px;
        font-size: 11px;
        font-weight: 650;
        border-radius: 999px;
        padding: 1px 8px;
        border: 1px solid var(--fl-border);
        white-space: nowrap;
        color: var(--fl-waiting);
    }
    .fl-badge::before {
        content: '';
        flex: none;
        width: 8px;
        height: 8px;
        box-sizing: border-box;
        border-radius: 50%;
        border: 2px solid var(--fl-waiting-mark);
    }
    .fl-badge.ok,
    .fl-badge.succeeded {
        color: var(--fl-good);
        border-color: var(--fl-good);
    }
    .fl-badge.ok::before,
    .fl-badge.succeeded::before {
        border-color: var(--fl-ok-mark);
        background: var(--fl-ok-mark);
    }
    .fl-badge.failed,
    .fl-badge.cancelled {
        color: var(--fl-bad);
        border-color: var(--fl-bad);
    }
    .fl-badge.failed::before,
    .fl-badge.cancelled::before {
        border-radius: 1px;
        border-color: var(--fl-bad);
        background: var(--fl-bad);
    }
    .fl-badge.running,
    .fl-badge.queued {
        color: var(--fl-primary);
        border-color: var(--fl-primary);
    }
    .fl-badge.running::before,
    .fl-badge.queued::before {
        border-width: 3px;
        border-color: var(--fl-run);
    }
    .fl-badge.waiting {
        color: var(--fl-warn);
        border-color: var(--fl-warn);
    }
    .fl-badge.waiting::before {
        border: 0;
        border-radius: 0;
        background: var(--fl-warn);
        clip-path: polygon(50% 0, 100% 100%, 0 100%);
    }
    /* The same table look as the rest of the workspace (antd's tables): 14 px cells, quiet uppercase headings. */
    .fl-table {
        width: 100%;
        border-collapse: collapse;
        font-size: 14px;
    }
    .fl-table th,
    .fl-table td {
        border-bottom: 1px solid var(--fl-border);
        padding: 10px 12px;
        text-align: left;
    }
    .fl-table th {
        font-size: 12px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        color: var(--fl-muted);
        background: var(--cairn-sunken, #F8FAFC);
    }
    .fl-table td .fl-muted {
        font-size: 13px;
    }
    .fl-pre {
        background: var(--fl-canvas);
        border: 1px solid var(--fl-border);
        border-radius: 8px;
        padding: 8px;
        font-size: 12px;
        max-height: 260px;
        overflow: auto;
        white-space: pre-wrap;
        word-break: break-word;
        color: var(--fl-text);
    }
    .fl-gallery {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
        gap: 12px;
    }
    .fl-diff-add {
        color: var(--fl-good);
    }
    .fl-diff-del {
        color: var(--fl-bad);
    }
    .fl-diff-chg {
        color: var(--fl-warn);
    }
    .fl-sr {
        position: absolute;
        width: 1px;
        height: 1px;
        overflow: hidden;
        clip: rect(0 0 0 0);
        white-space: nowrap;
    }
`;
