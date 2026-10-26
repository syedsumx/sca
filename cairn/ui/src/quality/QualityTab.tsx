import React from 'react';
import { Space } from 'antd';

import { DatasetHealthBadge } from './DatasetHealthBadge';
import { QualityDashboard } from './QualityDashboard';

export interface QualityTabProps {
    /** URN of the dataset whose profile page hosts this tab. */
    urn: string;
}

/**
 * Entity-profile tab for datasets. Register it in cairn-web-ui's DatasetEntity `getProfileTabs`
 * (see README) next to the native "Quality" tab.
 */
export function QualityTab({ urn }: QualityTabProps) {
    return (
        <Space direction="vertical" style={{ width: '100%', padding: 16 }} size="middle">
            <span>
                Overall health: <DatasetHealthBadge datasetUrn={urn} />
            </span>
            <QualityDashboard datasetUrn={urn} embedded />
        </Space>
    );
}

export default QualityTab;
