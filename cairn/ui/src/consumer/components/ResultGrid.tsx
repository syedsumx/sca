import { Button, Input, Space, Table, Tag, Tooltip, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import React, { useMemo, useState } from 'react';

import type { DataResult } from '../api/types';
import { cellText, columnStats, type ColumnFilters, matching, shownColumns, toTsv } from '../sql/resultStats';
import { Muted } from './tokens';

async function copy(text: string, what: string) {
    try {
        await navigator.clipboard.writeText(text);
        message.success(`Copied ${what}`);
    } catch {
        message.warning('The browser did not allow copying: select the text and copy it instead');
    }
}

/** Numbers right-aligned and compared as numbers; everything else as text; nulls first. */
function compare(a: unknown, b: unknown): number {
    if (a === null || a === undefined) return b === null || b === undefined ? 0 : -1;
    if (b === null || b === undefined) return 1;
    if (typeof a === 'number' && typeof b === 'number') return a - b;
    return cellText(a).localeCompare(cellText(b), undefined, { numeric: true });
}

/**
 * A result's rows (L163): find a column by name, filter any column (``null`` finds empty cells), sort by a column,
 * and copy a cell, a column, the rows shown, or everything as tab-separated text that pastes into a spreadsheet.
 * Masked columns say so in their header.
 */
export function ResultGrid({ result, filters, onFilters }: {
    result: DataResult;
    filters: ColumnFilters;
    onFilters: (f: ColumnFilters) => void;
}) {
    const [search, setSearch] = useState('');
    const shown = shownColumns(result.columns, search);
    const rowsShown = useMemo(() => matching(result.columns, result.rows, filters), [result, filters]);
    const filtered = Object.values(filters).some((f) => f.trim());
    const masked = new Set(result.masked_columns);

    const data = rowsShown.map((n) => ({ __row: n, cells: result.rows[n] }));
    const columns: ColumnsType<{ __row: number; cells: unknown[] }> = shown.map((c) => {
        const i = result.columns.indexOf(c);
        const type = result.types?.[i];
        return {
            key: c,
            title: (
                <span title={type ? `${c} (${type})` : c}>
                    {c}
                    {masked.has(c) && <Tag style={{ marginInlineStart: 6 }}>masked</Tag>}
                </span>
            ),
            sorter: (a, b) => compare(a.cells[i], b.cells[i]),
            align: typeof result.rows.find((r) => r[i] !== null && r[i] !== undefined)?.[i] === 'number' ? 'right' : 'left',
            filteredValue: filters[c] ? [filters[c]] : null,
            filterDropdown: ({ close }) => (
                <div style={{ padding: 8, width: 220 }} onKeyDown={(e) => e.stopPropagation()}>
                    <Input
                        size="small"
                        autoFocus
                        aria-label={`Filter ${c}`}
                        placeholder="contains… (null: empty)"
                        value={filters[c] ?? ''}
                        onChange={(e) => onFilters({ ...filters, [c]: e.target.value })}
                        onPressEnter={() => close()}
                    />
                    <Space style={{ marginTop: 8 }}>
                        <Button size="small" onClick={() => { onFilters({ ...filters, [c]: '' }); close(); }}>Clear</Button>
                        <Button size="small" onClick={() => void copy(toTsv([c], rowsShown.map((n) => [result.rows[n][i]])), `${c}`)}>
                            Copy column
                        </Button>
                    </Space>
                </div>
            ),
            render: (_: unknown, row) => {
                const v = row.cells[i];
                return (
                    <span className="cairn-cell" onDoubleClick={() => void copy(cellText(v), 'the value')}
                        title="Double-click to copy">
                        {v === null || v === undefined ? <Muted>null</Muted> : cellText(v)}
                    </span>
                );
            },
        };
    });


    return (
        <div data-testid="result-grid">
            <Space wrap style={{ marginBottom: 8 }}>
                <Input.Search size="small" allowClear placeholder="Find a column" aria-label="Find a column" value={search}
                    onChange={(e) => setSearch(e.target.value)} style={{ width: 200 }} />
                <Muted>
                    {filtered ? `${rowsShown.length} of ${result.rows.length} rows match` : `${result.rows.length} rows`}
                    {shown.length < result.columns.length ? ` · ${shown.length} of ${result.columns.length} columns` : ''}
                </Muted>
                {filtered && <Button size="small" onClick={() => onFilters({})}>Clear filters</Button>}
                <Tooltip title="Tab-separated: pastes into a spreadsheet as cells">
                    <Button size="small" onClick={() => void copy(toTsv(shown, rowsShown.map((n) =>
                        shown.map((c) => result.rows[n][result.columns.indexOf(c)]))), `${rowsShown.length} rows`)}>
                        Copy rows
                    </Button>
                </Tooltip>
            </Space>
            <Table
                size="small"
                rowKey="__row"
                scroll={{ x: true }}
                pagination={{ pageSize: 50, hideOnSinglePage: true, showSizeChanger: false }}
                dataSource={data}
                columns={columns}
            />
            <ColumnSummary columns={shown} all={result.columns} rows={rowsShown.map((n) => result.rows[n])} />
        </div>
    );
}

/** What each column holds (over the matching rows). */
function ColumnSummary({ columns, all, rows }: { columns: string[]; all: string[]; rows: unknown[][] }) {
    const [open, setOpen] = useState(false);              // worked out and drawn only when opened
    const stats = useMemo(() => (open ? columns.map((c) => columnStats(c, all.indexOf(c), rows)) : []), [open, columns, all, rows]);
    const fmt = (v: unknown) => (typeof v === 'number' ? (Number.isInteger(v) ? String(v) : v.toFixed(2)) : v == null ? '' : String(v));
    return (
        <details style={{ marginTop: 8 }} onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
            <summary style={{ cursor: 'pointer' }}>Column summary</summary>
            {open && <Table
                size="small"
                rowKey="column"
                pagination={false}
                scroll={{ x: true }}
                dataSource={stats}
                columns={[
                    { title: 'Column', dataIndex: 'column' },
                    { title: 'Holds', dataIndex: 'kind' },
                    { title: 'Empty', render: (_, s) => `${s.nulls} of ${s.count}` },
                    { title: 'Distinct', dataIndex: 'distinct' },
                    { title: 'Min', render: (_, s) => fmt(s.min) },
                    { title: 'Max', render: (_, s) => fmt(s.max) },
                    { title: 'Mean', render: (_, s) => fmt(s.mean) },
                    { title: 'Most common', render: (_, s) => s.top.slice(0, 3).map((t) => `${t.value} (${t.count})`).join(', ') },
                ]}
            />}
        </details>
    );
}
