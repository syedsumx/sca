import styled, { css } from 'styled-components';

/** Design tokens for consumer surfaces (aligned with DataHub alchemy-components spacing/greys). */
export const tokens = {
    color: {
        primary: '#533fd1',
        primarySoft: '#f1effd',
        text: '#1f2937',
        textMuted: '#6b7280',
        border: '#e5e7eb',
        surface: '#ffffff',
        surfaceAlt: '#f9fafb',
        good: '#2e7d32',
        warn: '#b26a00',
        bad: '#c62828',
        focus: '#7c6ae8',
    },
    radius: { sm: '6px', md: '10px', lg: '14px' },
    shadow: { card: '0 1px 2px rgba(16,24,40,.06), 0 1px 3px rgba(16,24,40,.08)', hover: '0 6px 16px rgba(16,24,40,.12)' },
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
    .aegis-markdown p {
        margin: 0 0 8px;
    }
    .aegis-markdown pre {
        background: ${tokens.color.surfaceAlt};
        padding: 8px 12px;
        border-radius: ${tokens.radius.sm};
        overflow: auto;
    }
    .aegis-mention {
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
    animation: aegis-shimmer 1.2s ease infinite;
    @keyframes aegis-shimmer {
        0% {
            background-position: 100% 50%;
        }
        100% {
            background-position: 0 50%;
        }
    }
`;
