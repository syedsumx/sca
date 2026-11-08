import styled, { createGlobalStyle, css } from 'styled-components';

/** Design tokens for consumer surfaces (aligned with Cairn Core alchemy-components spacing/greys). */
export const tokens = {
    color: {
        primary: '#5145e5',
        primaryHover: '#4338ca',
        primarySoft: '#eef0ff',
        text: '#0f172a',
        textMuted: '#556070',
        border: '#e6e8ef',
        surface: '#ffffff',
        surfaceAlt: '#f6f7fb',
        sidebar: '#0f1222',
        sidebarText: '#a7adc6',
        sidebarActive: '#ffffff',
        good: '#15803d',
        warn: '#b45309',
        bad: '#b91c1c',
        focus: '#6d63f0',
    },
    radius: { sm: '8px', md: '12px', lg: '16px' },
    shadow: {
        card: '0 1px 2px rgba(15,23,42,.04), 0 1px 3px rgba(15,23,42,.06)',
        hover: '0 8px 24px rgba(15,23,42,.10)',
    },
    font: '"Inter", ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
    space: (n: number) => `${n * 4}px`,
};

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
 * A modern look for the antd components inside the workspace only (scoped to `.cairn-consumer`, so a host shell
 * keeps its own styles): softer radii, quieter borders, clearer table headers and tabs.
 */
export const ConsumerTheme = createGlobalStyle`
    .cairn-consumer {
        font-family: ${tokens.font};
        -webkit-font-smoothing: antialiased;
    }
    .cairn-consumer h1, .cairn-consumer h2, .cairn-consumer h3 { letter-spacing: -0.01em; color: ${tokens.color.text}; }
    .cairn-consumer .ant-btn { border-radius: ${tokens.radius.sm}; font-weight: 500; box-shadow: none; }
    .cairn-consumer .ant-btn-primary:not([disabled]) { color: #fff; background: ${tokens.color.primary}; border-color: ${tokens.color.primary}; }
    .cairn-consumer .ant-btn-primary:not([disabled]) > span { color: #fff; }
    .cairn-consumer .ant-btn-primary:not([disabled]):hover, .cairn-consumer .ant-btn-primary:not([disabled]):focus {
        color: #fff;
        background: ${tokens.color.primaryHover}; border-color: ${tokens.color.primaryHover};
    }
    .cairn-consumer .ant-btn-primary[disabled] { background: #f1f2f6; border-color: ${tokens.color.border}; }
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
        background: #f8f9fc; color: ${tokens.color.textMuted}; font-weight: 600; font-size: 12px;
        text-transform: uppercase; letter-spacing: 0.04em; border-bottom-color: ${tokens.color.border};
    }
    .cairn-consumer .ant-table-thead > tr > th::before { display: none; }
    .cairn-consumer .ant-table-tbody > tr > td { border-bottom-color: #eef0f5; }
    .cairn-consumer .ant-table-tbody > tr.ant-table-row:hover > td { background: #fafbff; }
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
    .cairn-consumer .ant-tag-green, .cairn-consumer .ant-tag-success { color: #166534; background: #ecfdf3; border-color: #bbe5c8; }
    .cairn-consumer .ant-tag-gold, .cairn-consumer .ant-tag-warning, .cairn-consumer .ant-tag-orange { color: #854d0e; background: #fffbeb; border-color: #f5dfa3; }
    .cairn-consumer .ant-tag-red, .cairn-consumer .ant-tag-error { color: #991b1b; background: #fef2f2; border-color: #f5c2c2; }
    .cairn-consumer .ant-tag-blue, .cairn-consumer .ant-tag-processing { color: #1e40af; background: #eff6ff; border-color: #c3d6fb; }
    .cairn-consumer .ant-tag-purple { color: #5b21b6; }
    .cairn-consumer .sr-only {
        position: absolute !important; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden;
        clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0;
    }
    .cairn-consumer .ant-select-selection-placeholder, .cairn-consumer .ant-input::placeholder { color: #5d6475; }
    .cairn-consumer .ant-list-item-meta-description, .cairn-consumer .ant-typography-secondary,
    .cairn-consumer .ant-empty-description { color: ${tokens.color.textMuted}; }
    .cairn-consumer .ant-btn-dangerous.ant-btn-text, .cairn-consumer .ant-btn-dangerous.ant-btn-link,
    .cairn-consumer .ant-btn-dangerous.ant-btn-default { color: #b91c1c; border-color: #f0b4b4; }
    .cairn-consumer .ant-btn-link:not(.ant-btn-dangerous):not([disabled]) { color: ${tokens.color.primary}; }
    .cairn-consumer .ant-tag-cyan { color: #155e75; }
    .cairn-consumer main a:hover { color: ${tokens.color.primaryHover}; }
`;
