import React from 'react';
import { Button, Empty, Spin } from 'antd';
import styled from 'styled-components';

import { ErrorNotice } from '../consumer/components/ErrorNotice';

const Center = styled.div`
    display: flex;
    justify-content: center;
    align-items: center;
    padding: 48px 16px;
`;

export function LoadingState({ label = 'Loading…' }: { label?: string }) {
    return (
        <Center role="status" aria-live="polite">
            <Spin tip={label} />
        </Center>
    );
}

/** A failed read in the shared presenter's words, with Retry. */
export function ErrorState({ error, onRetry }: { error: Error; onRetry?: () => void }) {
    return (
        <div>
            <ErrorNotice error={error} />
            {onRetry && (
                <Button size="small" style={{ marginTop: 8 }} onClick={onRetry}>
                    Retry
                </Button>
            )}
        </div>
    );
}

export function EmptyState({ description, children }: { description: React.ReactNode; children?: React.ReactNode }) {
    return (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={description}>
            {children}
        </Empty>
    );
}

export const PageContainer = styled.div`
    padding: 24px;
    display: flex;
    flex-direction: column;
    gap: 16px;
    max-width: 1440px;
    margin: 0 auto;
    width: 100%;
`;

export const PageHeader = styled.div`
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;

    h1 {
        font-size: 20px;
        font-weight: 600;
        margin: 0;
    }
`;

export const Mono = styled.code`
    font-family: SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 12px;
    word-break: break-all;
`;
