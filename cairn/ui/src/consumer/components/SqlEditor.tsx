import { Input } from 'antd';
import type { TextAreaRef } from 'antd/lib/input/TextArea';
import React, { useCallback, useEffect, useRef, useState } from 'react';

import type { SchemaTable } from '../api/types';
import { accept, complete, type Completion } from '../sql/complete';

const KIND_LABEL = { table: 'table', schema: 'schema', column: 'column', keyword: 'keyword', function: 'function' } as const;

interface Props {
    value: string;
    onChange: (sql: string) => void;
    tables: SchemaTable[];
    onRun?: () => void;
    placeholder?: string;
}

/** The SQL editor: a text area with autocomplete from the tables the person may query. Suggestions open as
 * you type a name or a dot (and with Ctrl+Space); arrows move, Enter or Tab takes one, Escape closes. */
export function SqlEditor({ value, onChange, tables, onRun, placeholder }: Props) {
    const ref = useRef<TextAreaRef>(null);
    const [open, setOpen] = useState<Completion | null>(null);
    const [active, setActive] = useState(0);
    const caret = useRef<number | null>(null);

    const area = () => ref.current?.resizableTextArea?.textArea as HTMLTextAreaElement | undefined;

    const suggest = useCallback((sql: string, at: number) => {
        const c = complete(sql, at, tables);
        setOpen(c.items.length ? c : null);
        setActive(0);
    }, [tables]);

    useEffect(() => {
        if (caret.current === null) return;
        const el = area();
        if (el) {
            el.focus();
            el.setSelectionRange(caret.current, caret.current);
        }
        caret.current = null;
    }, [value]);

    const take = (i: number) => {
        if (!open) return;
        const out = accept(value, open, open.items[i]);
        caret.current = out.cursor;
        setOpen(null);
        onChange(out.sql);
    };

    return (
        <div style={{ position: 'relative' }}>
            <Input.TextArea
                ref={ref}
                aria-label="SQL"
                aria-autocomplete="list"
                aria-expanded={!!open}
                aria-controls="sql-suggestions"
                value={value}
                onChange={(e) => {
                    const sql = e.target.value;
                    const at = e.target.selectionStart ?? sql.length;
                    onChange(sql);
                    const typed = sql.slice(at - 1, at);
                    if (/[\w."]/.test(typed) && sql.length > value.length) suggest(sql, at);
                    else setOpen(null);
                }}
                onKeyDown={(e) => {
                    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
                        e.preventDefault();
                        setOpen(null);
                        onRun?.();
                        return;
                    }
                    if (e.ctrlKey && e.key === ' ') {
                        e.preventDefault();
                        const el = e.currentTarget;
                        suggest(value, el.selectionStart ?? value.length);
                        return;
                    }
                    if (!open) return;
                    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                        e.preventDefault();
                        const n = open.items.length;
                        setActive((a) => (e.key === 'ArrowDown' ? (a + 1) % n : (a - 1 + n) % n));
                    } else if (e.key === 'Enter' || e.key === 'Tab') {
                        e.preventDefault();
                        take(active);
                    } else if (e.key === 'Escape') {
                        e.preventDefault();
                        setOpen(null);
                    }
                }}
                onBlur={() => setTimeout(() => setOpen(null), 150)}
                autoSize={{ minRows: 6, maxRows: 20 }}
                style={{ fontFamily: 'ui-monospace, Menlo, monospace', width: '100%', boxSizing: 'border-box' }}
                placeholder={placeholder}
            />
            {open && (
                <ul
                    id="sql-suggestions"
                    role="listbox"
                    aria-label="Suggestions"
                    style={{
                        position: 'absolute', zIndex: 20, left: 0, top: '100%', margin: 0, padding: 4, listStyle: 'none',
                        background: 'var(--cairn-surface, #fff)', border: '1px solid #d9d9d9', borderRadius: 6,
                        boxShadow: '0 4px 12px rgba(0,0,0,.12)', maxHeight: 260, overflowY: 'auto', minWidth: 320,
                        fontFamily: 'ui-monospace, Menlo, monospace', fontSize: 13,
                    }}>
                    {open.items.map((s, i) => (
                        <li
                            key={s.kind + s.label}
                            role="option"
                            aria-selected={i === active}
                            onMouseDown={(e) => {
                                e.preventDefault();
                                take(i);
                            }}
                            onMouseEnter={() => setActive(i)}
                            style={{
                                display: 'flex', justifyContent: 'space-between', gap: 12, padding: '3px 8px',
                                borderRadius: 4, cursor: 'pointer', background: i === active ? 'rgba(22,119,255,.12)' : undefined,
                            }}>
                            <span>{s.label}</span>
                            <span style={{ opacity: 0.6 }}>{s.detail ?? KIND_LABEL[s.kind]}</span>
                        </li>
                    ))}
                </ul>
            )}
        </div>
    );
}
