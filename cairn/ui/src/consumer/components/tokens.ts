import styled, { createGlobalStyle, css } from 'styled-components';

/**
 * Design tokens for the workspace (the platform design): a quiet ground, white surfaces, one blue accent, and status
 * colours that always come with a text label and a shape (see StatusBadge). The same values are CSS variables
 * (`--cairn-*`, `ThemeVariables`) so plain CSS, SVG and the dark theme use them too.
 */
export const tokens = {
    color: {
        primary: '#1D4ED8',
        primaryHover: '#1E3A8A',
        primarySoft: '#EFF6FF',
        text: '#0F172A',
        textMuted: '#475569',
        textSubtle: '#334155',
        border: '#E2E8F0',
        borderStrong: '#CBD5E1',
        surface: '#FFFFFF',
        surfaceAlt: '#F6F7F9',
        surfaceSunken: '#F8FAFC',
        sidebar: '#FFFFFF',
        sidebarText: '#334155',
        sidebarActive: '#1E3A8A',
        good: '#15803D',
        warn: '#B45309',
        bad: '#B91C1C',
        waiting: '#64748B',
        focus: '#1D4ED8',
    },
    /** Status pairs: background and text (4.5:1 or better), and the mark's colour (3:1 or better on white). */
    status: {
        ok: { bg: '#DCFCE7', fg: '#14532D', mark: '#16A34A' },
        warn: { bg: '#FEF3C7', fg: '#78350F', mark: '#B45309' },
        error: { bg: '#FEE2E2', fg: '#7F1D1D', mark: '#DC2626' },
        running: { bg: '#EFF6FF', fg: '#1E3A8A', mark: '#2563EB' },
        waiting: { bg: '#F1F5F9', fg: '#334155', mark: '#64748B' },
    },
    radius: { sm: '8px', md: '10px', lg: '14px' },
    shadow: {
        card: '0 1px 2px rgba(15,23,42,.05)',
        hover: '0 6px 20px rgba(15,23,42,.08)',
    },
    font: '"Inter", ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
    fontHeading: '"Space Grotesk", "Inter", ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, Arial, sans-serif',
    fontMono: '"JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
    /** The smallest a control people press may be (WCAG 2.5.5 target size). */
    control: 44,
    space: (n: number) => `${n * 4}px`,
};

/** The tokens as CSS variables (light). */
export const LIGHT_VARS = `
    --cairn-ground: ${tokens.color.surfaceAlt};
    --cairn-surface: ${tokens.color.surface};
    --cairn-sunken: ${tokens.color.surfaceSunken};
    --cairn-border: ${tokens.color.border};
    --cairn-border-strong: ${tokens.color.borderStrong};
    --cairn-text: ${tokens.color.text};
    --cairn-text-muted: ${tokens.color.textMuted};
    --cairn-text-subtle: ${tokens.color.textSubtle};
    --cairn-accent: ${tokens.color.primary};
    --cairn-accent-hover: ${tokens.color.primaryHover};
    --cairn-accent-tint: ${tokens.color.primarySoft};
    --cairn-on-accent: #FFFFFF;
    --cairn-focus: ${tokens.color.focus};
    --cairn-ok-bg: ${tokens.status.ok.bg};
    --cairn-ok-fg: ${tokens.status.ok.fg};
    --cairn-ok-mark: ${tokens.status.ok.mark};
    --cairn-warn-bg: ${tokens.status.warn.bg};
    --cairn-warn-fg: ${tokens.status.warn.fg};
    --cairn-warn-mark: ${tokens.status.warn.mark};
    --cairn-error-bg: ${tokens.status.error.bg};
    --cairn-error-fg: ${tokens.status.error.fg};
    --cairn-error-mark: ${tokens.status.error.mark};
    --cairn-running-bg: ${tokens.status.running.bg};
    --cairn-running-fg: ${tokens.status.running.fg};
    --cairn-running-mark: ${tokens.status.running.mark};
    --cairn-waiting-bg: ${tokens.status.waiting.bg};
    --cairn-waiting-fg: ${tokens.status.waiting.fg};
    --cairn-waiting-mark: ${tokens.status.waiting.mark};
    --cairn-radius-sm: ${tokens.radius.sm};
    --cairn-radius-md: ${tokens.radius.md};
    --cairn-radius-lg: ${tokens.radius.lg};
    --cairn-control: ${tokens.control}px;
    --cairn-font: ${tokens.font};
    --cairn-font-heading: ${tokens.fontHeading};
    --cairn-font-mono: ${tokens.fontMono};
    --cairn-code-keyword: #1E40AF;
    --cairn-code-string: #166534;
    --cairn-code-number: #9A3412;
    --cairn-code-comment: #5B6576;
    --cairn-code-function: #6B21A8;
    --cairn-code-selection: #DBEAFE;
`;

/** The dark values: the same roles, derived for a dark ground with the same contrast targets. */
export const DARK_VARS = `
    --cairn-ground: #0B1120;
    --cairn-surface: #111827;
    --cairn-sunken: #0F172A;
    --cairn-border: #263244;
    --cairn-border-strong: #334155;
    --cairn-text: #F1F5F9;
    --cairn-text-muted: #A8B3C4;
    --cairn-text-subtle: #CBD5E1;
    --cairn-accent: #93B4FF;
    --cairn-accent-hover: #BFD3FF;
    --cairn-accent-tint: #172554;
    --cairn-on-accent: #0B1120;
    --cairn-focus: #93B4FF;
    --cairn-ok-bg: #052E16;
    --cairn-ok-fg: #BBF7D0;
    --cairn-ok-mark: #4ADE80;
    --cairn-warn-bg: #422006;
    --cairn-warn-fg: #FDE68A;
    --cairn-warn-mark: #FBBF24;
    --cairn-error-bg: #450A0A;
    --cairn-error-fg: #FECACA;
    --cairn-error-mark: #F87171;
    --cairn-running-bg: #172554;
    --cairn-running-fg: #BFDBFE;
    --cairn-running-mark: #60A5FA;
    --cairn-waiting-bg: #1E293B;
    --cairn-waiting-fg: #CBD5E1;
    --cairn-waiting-mark: #94A3B8;
    --cairn-code-keyword: #93C5FD;
    --cairn-code-string: #86EFAC;
    --cairn-code-number: #FDBA74;
    --cairn-code-comment: #94A3B8;
    --cairn-code-function: #D8B4FE;
    --cairn-code-selection: #1E3A8A;
`;

/**
 * The CSS variables on the workspace's roots: light by default; dark inside an element with `data-theme="dark"`, or
 * `data-theme="auto"` when the system prefers dark (the switch the Flows workspace has).
 */
export const ThemeVariables = createGlobalStyle`
    .cairn-consumer, .cairn-shell, [data-cairn-theme] {
        ${LIGHT_VARS}
    }
    .cairn-consumer [data-theme='dark'], .cairn-shell [data-theme='dark'], [data-cairn-theme][data-theme='dark'] {
        ${DARK_VARS}
    }
    @media (prefers-color-scheme: dark) {
        .cairn-consumer [data-theme='auto'], .cairn-shell [data-theme='auto'], [data-cairn-theme][data-theme='auto'] {
            ${DARK_VARS}
        }
    }
`;

export const focusRing = css`
    &:focus-visible {
        outline: 2px solid ${tokens.color.focus};
        outline-offset: 2px;
    }
`;

export const Page = styled.div`
    max-width: 1280px;
    margin: 0 auto;
    padding: ${tokens.space(6)} ${tokens.space(6)} ${tokens.space(12)};
    color: ${tokens.color.text};
    @media (max-width: 600px) {
        padding: ${tokens.space(3)} ${tokens.space(4)} ${tokens.space(8)};
    }
    /* A page shown as another section's tab (Embedded): that section's page frames it. */
    [data-embedded='true'] && {
        max-width: none;
        margin: 0;
        padding: 0;
    }
    .cairn-markdown p {
        margin: 0 0 8px;
    }
    .cairn-markdown pre {
        background: ${tokens.color.surfaceAlt};
        padding: 8px 12px;
        border-radius: ${tokens.radius.sm};
        overflow: auto;
    }
    .cairn-mention {
        color: ${tokens.color.primary};
        font-weight: 500;
    }
`;

export const SectionTitle = styled.h2`
    font-size: 16px;
    font-weight: 600;
    margin: ${tokens.space(6)} 0 ${tokens.space(3)};
    display: flex;
    align-items: center;
    justify-content: space-between;
`;

export const Muted = styled.span`
    color: ${tokens.color.textMuted};
    /* A link in a line of muted text is told apart by its underline, not only by its colour (WCAG 1.4.1). */
    a {
        text-decoration: underline;
        text-decoration-thickness: 1px;
        text-underline-offset: 2px;
    }
`;

export const CardGrid = styled.div`
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
    gap: ${tokens.space(4)};
`;

export const Surface = styled.div`
    background: ${tokens.color.surface};
    border: 1px solid ${tokens.color.border};
    border-radius: ${tokens.radius.md};
    box-shadow: ${tokens.shadow.card};
`;

export const Skeleton = styled.div<{ h?: number; w?: string }>`
    height: ${(p) => p.h ?? 14}px;
    width: ${(p) => p.w ?? '100%'};
    border-radius: 6px;
    background: linear-gradient(90deg, #f0f0f3 25%, #e7e7ec 37%, #f0f0f3 63%);
    background-size: 400% 100%;
    animation: cairn-shimmer 1.2s ease infinite;
    @keyframes cairn-shimmer {
        0% {
            background-position: 100% 50%;
        }
        100% {
            background-position: 0 50%;
        }
    }
`;

/**
 * antd's primary colour is the Cairn accent everywhere antd draws it: buttons, switches, checkboxes, radios, pagination,
 * links, spinners, steps and pickers. antd 4's own primary (#1890ff) is only 3.2:1 on white, below WCAG AA. Popups and
 * dialogs render outside the workspace's element (in <body>), so the rules also apply under \`body.cairn-ui\`, the class
 * the workspace sets on the page while it is mounted (ConsumerApp).
 */
const PRIMARY_SCOPES = ['.cairn-consumer', 'body.cairn-ui'];
const inScope = (selectors: string) =>
    PRIMARY_SCOPES.flatMap((scope) => selectors.split(',').map((x) => `${scope} ${x.trim()}`)).join(', ');
export const PRIMARY_CSS = `
    ${inScope('.ant-btn-primary:not([disabled])')} {
        color: #fff; background: ${tokens.color.primary}; border-color: ${tokens.color.primary};
    }
    ${inScope('.ant-btn-primary:not([disabled]) > span')} { color: #fff; }
    ${inScope('.ant-btn-primary:not([disabled]):hover, .ant-btn-primary:not([disabled]):focus')} {
        color: #fff; background: ${tokens.color.primaryHover}; border-color: ${tokens.color.primaryHover};
    }
    ${inScope('.ant-btn-primary[disabled]')} { background: #f1f2f6; border-color: ${tokens.color.border}; }
    ${inScope('.ant-btn-default:not([disabled]):hover, .ant-btn-default:not([disabled]):focus, .ant-btn-dashed:not([disabled]):hover')} {
        color: ${tokens.color.primaryHover}; border-color: ${tokens.color.primary};
    }
    ${inScope('.ant-btn-link:not(.ant-btn-dangerous):not([disabled])')} { color: ${tokens.color.primary}; }
    ${inScope('.ant-btn-link:not(.ant-btn-dangerous):not([disabled]):hover')} { color: ${tokens.color.primaryHover}; }
    ${inScope('.ant-switch-checked')} { background: ${tokens.color.primary}; }
    ${inScope('.ant-checkbox-checked .ant-checkbox-inner, .ant-checkbox-indeterminate .ant-checkbox-inner::after')} {
        background-color: ${tokens.color.primary}; border-color: ${tokens.color.primary};
    }
    ${inScope('.ant-checkbox-wrapper:hover .ant-checkbox-inner, .ant-checkbox:hover .ant-checkbox-inner, .ant-radio-wrapper:hover .ant-radio-inner')} {
        border-color: ${tokens.color.primary};
    }
    ${inScope('.ant-radio-checked .ant-radio-inner')} { border-color: ${tokens.color.primary}; }
    ${inScope('.ant-radio-inner::after')} { background-color: ${tokens.color.primary}; }
    ${inScope('.ant-radio-button-wrapper-checked:not(.ant-radio-button-wrapper-disabled)')} {
        color: ${tokens.color.primary}; border-color: ${tokens.color.primary};
    }
    ${inScope('.ant-radio-button-wrapper-checked:not(.ant-radio-button-wrapper-disabled)::before')} { background-color: ${tokens.color.primary}; }
    ${inScope('.ant-radio-group-solid .ant-radio-button-wrapper-checked:not(.ant-radio-button-wrapper-disabled)')} {
        color: #fff; background: ${tokens.color.primary};
    }
    ${inScope('.ant-pagination-item-active')} { border-color: ${tokens.color.primary}; }
    ${inScope('.ant-pagination-item-active a, .ant-pagination-item:hover a')} { color: ${tokens.color.primary}; }
    ${inScope('.ant-spin, .ant-spin-dot-item')} { color: ${tokens.color.primary}; }
    ${inScope('.ant-spin-dot-item')} { background-color: ${tokens.color.primary}; }
    ${inScope('.ant-steps-item-process .ant-steps-item-icon')} { background: ${tokens.color.primary}; border-color: ${tokens.color.primary}; }
    ${inScope('.ant-steps-item-finish .ant-steps-item-icon')} { border-color: ${tokens.color.primary}; }
    ${inScope('.ant-steps-item-finish .ant-steps-item-icon > .ant-steps-icon')} { color: ${tokens.color.primary}; }
    ${inScope('.ant-select-item-option-selected:not(.ant-select-item-option-disabled) .ant-select-item-option-state')} {
        color: ${tokens.color.primary};
    }
    ${inScope('.ant-picker-cell-in-view.ant-picker-cell-selected .ant-picker-cell-inner, .ant-slider-track')} {
        background: ${tokens.color.primary};
    }
    ${inScope('.ant-slider-handle')} { border-color: ${tokens.color.primary}; }
    ${inScope('.ant-dropdown-menu-item-selected, .ant-menu-item-selected, .ant-menu-item-selected a')} { color: ${tokens.color.primary}; }
    ${inScope('.ant-progress-bg')} { background-color: ${tokens.color.primary}; }
    ${inScope('.ant-progress-success-bg, .ant-progress-status-success .ant-progress-bg')} { background-color: ${tokens.status.ok.mark}; }
    ${inScope('.ant-typography a, a.ant-typography, .ant-modal a, .ant-drawer a, .ant-popover a')} { color: ${tokens.color.primary}; }
    ${inScope('.ant-tabs-ink-bar')} { background: ${tokens.color.primary}; }
    ${inScope('.ant-tabs-tab.ant-tabs-tab-active .ant-tabs-tab-btn, .ant-tabs-tab:hover')} { color: ${tokens.color.primary}; }
`;

/**
 * A modern look for the antd components inside the workspace only (scoped to `.cairn-consumer`, so a host shell
 * keeps its own styles): softer radii, quieter borders, clearer table headers and tabs.
 */
export const ConsumerTheme = createGlobalStyle`
    ${PRIMARY_CSS}
    .cairn-consumer {
        font-family: ${tokens.font};
        -webkit-font-smoothing: antialiased;
    }
    .cairn-consumer h1, .cairn-consumer h2, .cairn-consumer h3 { letter-spacing: -0.01em; color: ${tokens.color.text}; }
    .cairn-consumer main h1 { font-family: ${tokens.fontHeading}; letter-spacing: -0.015em; }
    .cairn-consumer .ant-btn { border-radius: ${tokens.radius.sm}; font-weight: 500; box-shadow: none; }
    /* Controls people press are at least 44 px tall (the compact "small" size inside tables and toolbars stays dense). */
    .cairn-consumer .ant-btn:not(.ant-btn-sm):not(.ant-btn-link):not(.ant-btn-text):not(.ant-input-search-button):not(.ant-btn-icon-only) {
        min-height: ${tokens.control}px; padding-left: 16px; padding-right: 16px;
        display: inline-flex; align-items: center; justify-content: center;
    }
    .cairn-consumer .ant-btn-icon-only:not(.ant-btn-sm):not(.ant-input-search-button) { min-width: ${tokens.control}px; min-height: ${tokens.control}px; }
    .cairn-consumer .ant-input-search-button:not(.ant-btn-sm) { min-height: ${tokens.control}px; }
    .cairn-consumer .ant-input-affix-wrapper:not(.ant-input-affix-wrapper-sm),
    .cairn-consumer .ant-input:not(.ant-input-sm):not(textarea):not(.ant-input-affix-wrapper .ant-input) {
        min-height: ${tokens.control}px;
    }
    .cairn-consumer .ant-select:not(.ant-select-sm):not(.ant-select-customize-input) > .ant-select-selector {
        min-height: ${tokens.control}px; align-items: center;
    }
    .cairn-consumer .ant-select-single:not(.ant-select-sm):not(.ant-select-customize-input) > .ant-select-selector .ant-select-selection-search-input {
        height: ${tokens.control - 2}px;
    }
    .cairn-consumer .ant-picker:not(.ant-picker-small) { min-height: ${tokens.control}px; }
    .cairn-consumer .ant-radio-button-wrapper:not(.ant-radio-button-wrapper-sm) {
        height: ${tokens.control}px; line-height: ${tokens.control - 2}px;
    }
    .cairn-consumer .ant-tabs-tab { min-height: ${tokens.control}px; }
    .cairn-consumer .ant-input, .cairn-consumer .ant-input-affix-wrapper, .cairn-consumer .ant-input-number,
    .cairn-consumer .ant-select:not(.ant-select-customize-input) .ant-select-selector, .cairn-consumer .ant-picker {
        border-radius: ${tokens.radius.sm};
        border-color: ${tokens.color.border};
    }
    .cairn-consumer .ant-input:focus, .cairn-consumer .ant-input-focused, .cairn-consumer .ant-input-affix-wrapper-focused,
    .cairn-consumer .ant-select-focused .ant-select-selector {
        border-color: ${tokens.color.primary} !important;
        box-shadow: 0 0 0 3px ${tokens.color.primarySoft} !important;
    }
    .cairn-consumer .ant-card { border-radius: ${tokens.radius.md}; border-color: ${tokens.color.border}; box-shadow: ${tokens.shadow.card}; }
    .cairn-consumer .ant-card-head { border-bottom-color: ${tokens.color.border}; }
    .cairn-consumer .ant-table { border-radius: ${tokens.radius.md}; }
    .cairn-consumer .ant-table-container { border-radius: ${tokens.radius.md}; }
    .cairn-consumer .ant-table-thead > tr > th {
        background: ${tokens.color.surfaceSunken}; color: ${tokens.color.textMuted}; font-weight: 600; font-size: 12px;
        text-transform: uppercase; letter-spacing: 0.04em; border-bottom-color: ${tokens.color.border};
    }
    .cairn-consumer .ant-table-thead > tr > th::before { display: none; }
    .cairn-consumer .ant-table-tbody > tr > td { border-bottom-color: #F1F5F9; }
    .cairn-consumer .ant-table-tbody > tr.ant-table-row:hover > td { background: ${tokens.color.surfaceSunken}; }
    .cairn-consumer .ant-tabs-ink-bar { background: ${tokens.color.primary}; height: 2px !important; border-radius: 2px; }
    .cairn-consumer .ant-tabs-tab.ant-tabs-tab-active .ant-tabs-tab-btn { color: ${tokens.color.primary}; font-weight: 600; }
    .cairn-consumer .ant-tabs-tab:hover { color: ${tokens.color.primary}; }
    .cairn-consumer .ant-tabs-top > .ant-tabs-nav::before { border-bottom-color: ${tokens.color.border}; }
    .cairn-consumer .ant-tag { border-radius: 999px; padding: 0 8px; }
    .cairn-consumer .ant-modal-content, .cairn-consumer .ant-drawer-content { border-radius: ${tokens.radius.md}; }
    .cairn-consumer .ant-alert { border-radius: ${tokens.radius.sm}; }
    .cairn-consumer .ant-list-bordered, .cairn-consumer .ant-collapse { border-radius: ${tokens.radius.md}; }
    .cairn-consumer main a { color: ${tokens.color.primary}; }
    .cairn-consumer main p a, .cairn-consumer main li a, .cairn-consumer main td a {
        text-decoration: underline;
        text-decoration-thickness: 1px;
        text-underline-offset: 2px;
    }
    /* antd's coloured tags are too light to read (WCAG AA): the same hues, darker text. */
    .cairn-consumer .ant-tag-green, .cairn-consumer .ant-tag-success { color: ${tokens.status.ok.fg}; background: ${tokens.status.ok.bg}; border-color: #BBF7D0; }
    .cairn-consumer .ant-tag-gold, .cairn-consumer .ant-tag-warning, .cairn-consumer .ant-tag-orange { color: ${tokens.status.warn.fg}; background: ${tokens.status.warn.bg}; border-color: #FDE68A; }
    .cairn-consumer .ant-tag-red, .cairn-consumer .ant-tag-error { color: ${tokens.status.error.fg}; background: ${tokens.status.error.bg}; border-color: #FECACA; }
    .cairn-consumer .ant-tag-blue, .cairn-consumer .ant-tag-processing { color: ${tokens.status.running.fg}; background: ${tokens.status.running.bg}; border-color: #BFDBFE; }
    .cairn-consumer .ant-tag-purple { color: #5b21b6; }
    .cairn-consumer .sr-only {
        position: absolute !important; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden;
        clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0;
    }
    .cairn-consumer .ant-select-selection-placeholder, .cairn-consumer .ant-input::placeholder { color: #56606F; }
    .cairn-consumer .ant-list-item-meta-description, .cairn-consumer .ant-typography-secondary,
    .cairn-consumer .ant-empty-description { color: ${tokens.color.textMuted}; }
    .cairn-consumer .ant-btn-dangerous.ant-btn-text, .cairn-consumer .ant-btn-dangerous.ant-btn-link,
    .cairn-consumer .ant-btn-dangerous.ant-btn-default { color: #b91c1c; border-color: #f0b4b4; }
    .cairn-consumer .ant-btn-link:not(.ant-btn-dangerous):not([disabled]) { color: ${tokens.color.primary}; }
    .cairn-consumer .ant-tag-cyan { color: #155e75; }
    .cairn-consumer main a:hover { color: ${tokens.color.primaryHover}; }
    /* Keyboard focus is always visible (WCAG 2.4.7), whatever a component's own styles do. */
    .cairn-consumer a:focus-visible, .cairn-consumer button:focus-visible, .cairn-consumer [role='button']:focus-visible,
    .cairn-consumer [role='tab']:focus-visible, .cairn-consumer [role='option']:focus-visible,
    .cairn-consumer [tabindex]:not([tabindex='-1']):focus-visible, .cairn-consumer summary:focus-visible,
    .cairn-shell a:focus-visible, .cairn-shell button:focus-visible {
        outline: 2px solid ${tokens.color.focus};
        outline-offset: 2px;
    }
    .cairn-consumer .ant-tabs-tab-btn:focus-visible, .cairn-consumer .ant-checkbox-input:focus-visible + .ant-checkbox-inner,
    .cairn-consumer .ant-radio-input:focus-visible + .ant-radio-inner, .cairn-consumer .ant-switch:focus-visible {
        outline: 2px solid ${tokens.color.focus};
        outline-offset: 2px;
    }
`;
