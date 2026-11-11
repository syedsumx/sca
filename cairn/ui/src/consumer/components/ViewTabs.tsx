import { Tabs } from 'antd';
import React from 'react';
import { useHistory, useLocation } from 'react-router-dom';

export interface View {
    key: string;
    label: string;
    render: () => React.ReactNode;
}

/**
 * Tabs inside a section's tab (a section's `?tab=` is taken): the open one is kept in `?view=`, so every view has a
 * link of its own and old links redirect to it. Only the open view is rendered.
 */
export function ViewTabs({ views, param = 'view', label }: { views: View[]; param?: string; label: string }) {
    const location = useLocation();
    const history = useHistory();
    const asked = new URLSearchParams(location.search).get(param);
    const active = views.some((v) => v.key === asked) ? (asked as string) : views[0]?.key;
    return (
        <Tabs
            aria-label={label}
            size="small"
            activeKey={active}
            onChange={(k) => {
                const next = new URLSearchParams(location.search);
                next.set(param, k);
                history.replace({ pathname: location.pathname, search: `?${next}` });
            }}
            items={views.map((v) => ({ key: v.key, label: v.label, children: v.key === active ? v.render() : null }))}
        />
    );
}
