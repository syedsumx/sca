import styled from 'styled-components';

import { tokens } from '../components/tokens';

/**
 * The Flows workspace's look, in CSS variables from the design tokens: light by default, dark with
 * `data-theme="dark"` (or the system's preference with `data-theme="auto"`).
 */
const light = `
    --fl-bg: ${tokens.color.surfaceAlt};
    --fl-canvas: #fbfbfe;
    --fl-dot: #d9dcea;
    --fl-surface: ${tokens.color.surface};
    --fl-border: ${tokens.color.border};
    --fl-text: ${tokens.color.text};
    --fl-muted: ${tokens.color.textMuted};
    --fl-primary: ${tokens.color.primary};
    --fl-primary-soft: ${tokens.color.primarySoft};
    --fl-good: ${tokens.color.good};
    --fl-warn: ${tokens.color.warn};
    --fl-bad: ${tokens.color.bad};
    --fl-edge: #9aa3b8;
    --fl-shadow: 0 1px 2px rgba(15,23,42,.06), 0 4px 14px rgba(15,23,42,.06);
    --fl-focus: ${tokens.color.focus};
`;
const dark = `
    --fl-bg: #0b0e1a;
    --fl-canvas: #0f1324;
    --fl-dot: #262c45;
    --fl-surface: #161b30;
    --fl-border: #2a3150;
    --fl-text: #e7e9f3;
    --fl-muted: #a3aac4;
    --fl-primary: #8f87ff;
    --fl-primary-soft: #23264a;
    --fl-good: #4ade80;
    --fl-warn: #fbbf24;
    --fl-bad: #f87171;
    --fl-edge: #5b6482;
    --fl-shadow: 0 1px 2px rgba(0,0,0,.4), 0 6px 18px rgba(0,0,0,.35);
    --fl-focus: #a5a0ff;
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
    .fl-node.selected {
        border-color: var(--fl-primary);
        box-shadow: 0 0 0 3px var(--fl-primary-soft), var(--fl-shadow);
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
    .fl-node .fl-status {
        position: absolute;
        top: -9px;
        right: 10px;
        font-size: 10px;
        font-weight: 700;
        padding: 1px 7px;
        border-radius: 999px;
        background: var(--fl-surface);
        border: 1px solid var(--fl-border);
        text-transform: uppercase;
        letter-spacing: 0.04em;
    }
    .fl-node.st-ok {
        border-color: var(--fl-good);
    }
    .fl-node.st-ok .fl-status {
        color: var(--fl-good);
        border-color: var(--fl-good);
    }
    .fl-node.st-failed {
        border-color: var(--fl-bad);
    }
    .fl-node.st-failed .fl-status,
    .fl-node.st-cancelled .fl-status {
        color: var(--fl-bad);
        border-color: var(--fl-bad);
    }
    .fl-node.st-running,
    .fl-node.st-retrying {
        border-color: var(--fl-primary);
        animation: fl-pulse 1.4s ease-in-out infinite;
    }
    .fl-node.st-running .fl-status,
    .fl-node.st-retrying .fl-status {
        color: var(--fl-primary);
        border-color: var(--fl-primary);
    }
    .fl-node.st-waiting {
        border-color: var(--fl-warn);
    }
    .fl-node.st-waiting .fl-status {
        color: var(--fl-warn);
        border-color: var(--fl-warn);
    }
    .fl-node.st-skipped,
    .fl-node.st-pending {
        opacity: 0.6;
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
        font-size: 11px;
        font-weight: 650;
        border-radius: 999px;
        padding: 1px 8px;
        border: 1px solid var(--fl-border);
        white-space: nowrap;
    }
    .fl-badge.ok,
    .fl-badge.succeeded {
        color: var(--fl-good);
        border-color: var(--fl-good);
    }
    .fl-badge.failed,
    .fl-badge.cancelled {
        color: var(--fl-bad);
        border-color: var(--fl-bad);
    }
    .fl-badge.running,
    .fl-badge.queued {
        color: var(--fl-primary);
        border-color: var(--fl-primary);
    }
    .fl-badge.waiting {
        color: var(--fl-warn);
        border-color: var(--fl-warn);
    }
    .fl-table {
        width: 100%;
        border-collapse: collapse;
        font-size: 13px;
    }
    .fl-table th,
    .fl-table td {
        border-bottom: 1px solid var(--fl-border);
        padding: 6px 8px;
        text-align: left;
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
