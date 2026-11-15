import React, { useRef, useState } from 'react';

import { tokens } from './tokens';

/** One node of a {@link FileTree}: the shape of antd's tree data that the SQL editor's files use. */
export interface FileNode {
    key: string;
    title: React.ReactNode;
    /** Not given on a node that is not a leaf: loaded with ``loadData`` the first time it opens. */
    children?: FileNode[];
    isLeaf?: boolean;
    /** False: activating the node opens or closes it instead of choosing it. */
    selectable?: boolean;
}

interface Props {
    label: string;
    nodes: FileNode[];
    expanded: string[];
    onExpand: (key: string, expanded: boolean) => void;
    selected: string[];
    onSelect: (key: string) => void;
    loadData?: (key: string) => Promise<void>;
}

interface Row {
    node: FileNode;
    level: number;
    parent?: string;
}

/**
 * An ARIA tree (the WAI-ARIA tree view pattern): ``tree`` > ``treeitem`` > ``group``, one tab stop with the arrow
 * keys moving through what is shown, Right/Left to open, close and move between parent and child, Enter or Space
 * to choose a file or open a folder, Home/End. antd's Tree puts ARIA attributes on elements without a role and
 * other elements inside its ``tree``, which screen readers can't follow (axe: critical).
 */
export function FileTree({ label, nodes, expanded, onExpand, selected, onSelect, loadData }: Props) {
    const [focus, setFocus] = useState<string | undefined>();
    const [loading, setLoading] = useState<string[]>([]);
    const refs = useRef(new Map<string, HTMLLIElement>());
    const open = new Set(expanded);

    const rows: Row[] = [];
    const walk = (list: FileNode[], level: number, parent?: string) => {
        for (const node of list) {
            rows.push({ node, level, parent });
            if (open.has(node.key) && node.children) walk(node.children, level + 1, node.key);
        }
    };
    walk(nodes, 1);
    const current = rows.some((r) => r.node.key === focus) ? focus : rows[0]?.node.key;

    const toggle = async (node: FileNode, to: boolean) => {
        if (node.isLeaf) return;
        onExpand(node.key, to);
        if (to && node.children === undefined && loadData && !loading.includes(node.key)) {
            setLoading((l) => [...l, node.key]);
            try {
                await loadData(node.key);
            } finally {
                setLoading((l) => l.filter((k) => k !== node.key));
            }
        }
    };
    const activate = (node: FileNode) => {
        if (node.isLeaf && node.selectable !== false) onSelect(node.key);
        else if (!node.isLeaf) void toggle(node, !open.has(node.key));
    };
    const moveTo = (key: string | undefined) => {
        if (!key) return;
        setFocus(key);
        refs.current.get(key)?.focus();
    };

    const onKeyDown = (e: React.KeyboardEvent, row: Row) => {
        if (e.target !== e.currentTarget) return;           // keys typed in a file's own buttons are theirs
        const i = rows.findIndex((r) => r.node.key === row.node.key);
        const { node } = row;
        const isOpen = open.has(node.key);
        const handled = (() => {
            switch (e.key) {
                case 'ArrowDown': moveTo(rows[i + 1]?.node.key); return true;
                case 'ArrowUp': moveTo(rows[i - 1]?.node.key); return true;
                case 'Home': moveTo(rows[0]?.node.key); return true;
                case 'End': moveTo(rows[rows.length - 1]?.node.key); return true;
                case 'ArrowRight':
                    if (!node.isLeaf && !isOpen) void toggle(node, true);
                    else if (!node.isLeaf && node.children?.length) moveTo(node.children[0].key);
                    return true;
                case 'ArrowLeft':
                    if (!node.isLeaf && isOpen) void toggle(node, false);
                    else moveTo(row.parent);
                    return true;
                case 'Enter': case ' ': activate(node); return true;
                default: return false;
            }
        })();
        if (handled) {
            e.preventDefault();
            e.stopPropagation();
        }
    };

    const render = (list: FileNode[], level: number, parent?: string): React.ReactNode => list.map((node) => {
        const isOpen = open.has(node.key);
        const isSelected = selected.includes(node.key);
        const row = { node, level, parent };
        return (
            <li key={node.key} role="treeitem" aria-level={level} aria-expanded={node.isLeaf ? undefined : isOpen}
                aria-selected={node.isLeaf && node.selectable !== false ? isSelected : undefined}
                aria-busy={loading.includes(node.key) || undefined}
                tabIndex={node.key === current ? 0 : -1} className="cairn-tree-item"
                ref={(el) => { if (el) refs.current.set(node.key, el); else refs.current.delete(node.key); }}
                onFocus={(e) => { if (e.target === e.currentTarget) setFocus(node.key); }}
                onKeyDown={(e) => onKeyDown(e, row)}
                style={{ listStyle: 'none', outlineOffset: -2 }}>
                <div className="cairn-tree-row" onClick={(e) => { e.stopPropagation(); setFocus(node.key); activate(node); }}
                    style={{ display: 'flex', alignItems: 'center', gap: 4, padding: '2px 4px', paddingInlineStart: (level - 1) * 16 + 4,
                        cursor: 'pointer', borderRadius: 4, background: isSelected ? tokens.color.primarySoft : undefined }}>
                    <span className="cairn-tree-switcher" aria-hidden="true"
                        style={{ width: 14, flex: 'none', textAlign: 'center', color: tokens.color.textMuted }}>
                        {node.isLeaf ? '' : isOpen ? '▾' : '▸'}
                    </span>
                    <span style={{ minWidth: 0, flex: 1 }}>{node.title}</span>
                </div>
                {isOpen && node.children && node.children.length > 0 && (
                    <ul role="group" style={{ margin: 0, padding: 0 }}>{render(node.children, level + 1, node.key)}</ul>
                )}
            </li>
        );
    });

    return (
        <ul role="tree" aria-label={label} className="cairn-tree" style={{ margin: 0, padding: 0 }}>
            {render(nodes, 1)}
        </ul>
    );
}
