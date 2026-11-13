import { acceptCompletion, autocompletion, closeBrackets, closeBracketsKeymap, completionKeymap,
    type CompletionContext, type CompletionResult } from '@codemirror/autocomplete';
import { defaultKeymap, history, historyKeymap, indentWithTab } from '@codemirror/commands';
import { StandardSQL, sql as sqlLanguage } from '@codemirror/lang-sql';
import { bracketMatching, HighlightStyle, indentOnInput, syntaxHighlighting } from '@codemirror/language';
import { highlightSelectionMatches, search, searchKeymap } from '@codemirror/search';
import { Compartment, EditorSelection, EditorState, type Extension } from '@codemirror/state';
import { drawSelection, EditorView, highlightActiveLine, highlightActiveLineGutter, keymap, lineNumbers,
    placeholder as placeholderExt } from '@codemirror/view';
import { tags as t } from '@lezer/highlight';
import React, { forwardRef, useEffect, useImperativeHandle, useRef } from 'react';

import type { SchemaTable } from '../api/types';
import { complete, type Kind } from '../sql/complete';

/** What the SQL workspace asks of the editor: the selection, to run what is selected or the statement at the cursor. */
export interface SqlEditorHandle {
    /** The selection (``from === to``: the cursor). */
    selection(): { from: number; to: number };
    focus(): void;
    /** Puts ``text`` at the cursor (replacing the selection) and focuses the editor. */
    insert(text: string): void;
    /** Selects ``from``-``to`` (a statement whose run failed) and scrolls it into view. */
    select(from: number, to: number): void;
    /** Replaces ``from``-``to`` with ``text`` (an assistant's fix), undoable, and selects it. */
    replace(from: number, to: number, text: string): void;
}

interface Props {
    value: string;
    onChange: (sql: string) => void;
    tables: SchemaTable[];
    /** Ctrl/⌘+Enter: the selection or the statement at the cursor; Ctrl/⌘+Shift+Enter: all of it. */
    onRun?: (mode: 'current' | 'all') => void;
    /** Ctrl/⌘+S. */
    onSave?: () => void;
    placeholder?: string;
    /** For a form: the field's id, the label's id (its label names the editor), help text and error. */
    id?: string;
    labelledBy?: string;
    ariaLabel?: string;
    describedBy?: string;
    invalid?: boolean;
    /** Rows of text the editor shows before it scrolls (6 to 24 by default in the workspace). */
    minLines?: number;
    maxLines?: number;
}

const KIND_TYPE: Record<Kind, string> = { table: 'class', schema: 'namespace', column: 'property', keyword: 'keyword',
    function: 'function' };

/** Syntax colours from the workspace's tokens, so the dark theme is readable too. */
const HIGHLIGHT = HighlightStyle.define([
    { tag: [t.keyword, t.operatorKeyword, t.modifier, t.definitionKeyword], color: 'var(--cairn-code-keyword)', fontWeight: '600' },
    { tag: [t.string, t.special(t.string)], color: 'var(--cairn-code-string)' },
    { tag: [t.number, t.bool, t.null], color: 'var(--cairn-code-number)' },
    { tag: [t.lineComment, t.blockComment, t.comment], color: 'var(--cairn-code-comment)', fontStyle: 'italic' },
    { tag: [t.function(t.variableName), t.standard(t.name)], color: 'var(--cairn-code-function)' },
    { tag: [t.typeName], color: 'var(--cairn-code-function)' },
]);

const THEME = EditorView.theme({
    '&': {
        color: 'var(--cairn-text)', backgroundColor: 'var(--cairn-surface)', fontSize: '13px',
        border: '1px solid var(--cairn-border-strong)', borderRadius: 'var(--cairn-radius-md, 6px)',
    },
    '&.cm-focused': { outline: '2px solid var(--cairn-focus)', outlineOffset: '1px' },
    '&.cairn-invalid': { borderColor: 'var(--cairn-error-mark)' },
    '.cm-scroller': { fontFamily: 'var(--cairn-font-mono, ui-monospace, Menlo, monospace)', lineHeight: '1.55' },
    '.cm-content': { caretColor: 'var(--cairn-text)', padding: '6px 0' },
    '.cm-gutters': { backgroundColor: 'var(--cairn-sunken, transparent)', color: 'var(--cairn-text-muted)',
        borderRight: '1px solid var(--cairn-border)' },
    '.cm-activeLine': { backgroundColor: 'color-mix(in srgb, var(--cairn-accent-tint) 45%, transparent)' },
    '.cm-activeLineGutter': { backgroundColor: 'var(--cairn-accent-tint)', color: 'var(--cairn-text)' },
    '&.cm-focused .cm-selectionBackground, .cm-selectionBackground': { backgroundColor: 'var(--cairn-code-selection) !important' },
    '.cm-placeholder': { color: 'var(--cairn-text-muted)' },
    '.cm-tooltip': { backgroundColor: 'var(--cairn-surface)', color: 'var(--cairn-text)',
        border: '1px solid var(--cairn-border-strong)', borderRadius: '6px' },
    '.cm-tooltip-autocomplete > ul > li[aria-selected]': { backgroundColor: 'var(--cairn-accent-tint)', color: 'var(--cairn-text)' },
    '.cm-completionDetail': { color: 'var(--cairn-text-muted)', marginLeft: '12px', fontStyle: 'normal' },
    '.cm-panels': { backgroundColor: 'var(--cairn-sunken)', color: 'var(--cairn-text)' },
    '.cm-searchMatch': { backgroundColor: 'var(--cairn-warn-bg)', outline: '1px solid var(--cairn-warn-mark)' },
    '.cm-selectionMatch': { backgroundColor: 'color-mix(in srgb, var(--cairn-accent-tint) 70%, transparent)' },
    '.cm-matchingBracket': { backgroundColor: 'var(--cairn-accent-tint)', outline: '1px solid var(--cairn-accent)' },
});

/**
 * The SQL editor (L163): a code editor (CodeMirror) with SQL colours, line numbers, bracket matching, find and replace
 * (Ctrl/⌘+F), undo history, and suggestions from the tables the person may query (as they type a name or a dot, and
 * with Ctrl+Space; Enter or Tab takes one). Tab indents otherwise; Escape then Tab leaves the editor (the keyboard is
 * never trapped).
 */
export const SqlEditor = forwardRef<SqlEditorHandle, Props>(function SqlEditor(
    { value, onChange, tables, onRun, onSave, placeholder, id, labelledBy, ariaLabel, describedBy, invalid, minLines = 6,
        maxLines = 20 }, ref) {
    const host = useRef<HTMLDivElement>(null);
    const view = useRef<EditorView | null>(null);
    const latest = useRef({ onChange, onRun, onSave, tables });
    latest.current = { onChange, onRun, onSave, tables };
    // Texts this editor has reported (newest last): a value equal to one of them is the page catching up, possibly a
    // keystroke behind, and is never written back over what has been typed since.
    const emitted = useRef<string[]>([value]);
    const attrs = useRef(new Compartment());
    const ph = useRef(new Compartment());

    const contentAttrs = (): Extension => [
        EditorView.contentAttributes.of({
            ...(id ? { id } : {}),
            ...(labelledBy ? { 'aria-labelledby': labelledBy } : { 'aria-label': ariaLabel ?? 'SQL' }),
            ...(describedBy ? { 'aria-describedby': describedBy } : {}),
            ...(invalid ? { 'aria-invalid': 'true' } : {}),
            'aria-multiline': 'true',
        }),
        EditorView.editorAttributes.of({ class: invalid ? 'cairn-invalid' : '' }),
    ];

    useEffect(() => {
        if (!host.current) return undefined;
        const suggest = (ctx: CompletionContext): CompletionResult | null => {
            const text = ctx.state.doc.toString();
            const c = complete(text, ctx.pos, latest.current.tables);
            const typing = /[\w."]$/.test(text.slice(0, ctx.pos));
            if (!c.items.length || (!ctx.explicit && !typing)) return null;
            return {
                from: c.from, to: c.to, filter: false,
                options: c.items.map((s) => ({ label: s.label, apply: s.insert, type: KIND_TYPE[s.kind], detail: s.detail ?? s.kind })),
            };
        };
        const run = (mode: 'current' | 'all') => () => {
            if (!latest.current.onRun) return false;
            latest.current.onRun(mode);
            return true;
        };
        const lineHeight = 20;
        const v = new EditorView({
            parent: host.current,
            state: EditorState.create({
                doc: value,
                extensions: [
                    lineNumbers(), highlightActiveLineGutter(), highlightActiveLine(), history(), drawSelection(),
                    indentOnInput(), bracketMatching(), closeBrackets(), highlightSelectionMatches(), search({ top: true }),
                    sqlLanguage({ dialect: StandardSQL, upperCaseKeywords: true }),
                    syntaxHighlighting(HIGHLIGHT),
                    autocompletion({ override: [suggest], activateOnTyping: true, icons: false }),
                    keymap.of([
                        { key: 'Mod-Enter', run: run('current'), preventDefault: true },
                        { key: 'Shift-Mod-Enter', run: run('all'), preventDefault: true },
                        { key: 'Tab', run: acceptCompletion },          // with suggestions open, Tab takes one
                        { key: 'Mod-s', preventDefault: true, run: () => {
                            if (!latest.current.onSave) return false;
                            latest.current.onSave();
                            return true;
                        } },
                        ...closeBracketsKeymap, ...completionKeymap, ...searchKeymap, ...historyKeymap, ...defaultKeymap,
                        indentWithTab,
                    ]),
                    EditorView.updateListener.of((u) => {
                        if (!u.docChanged) return;
                        const text = u.state.doc.toString();
                        emitted.current = [...emitted.current.slice(-49), text];
                        latest.current.onChange(text);
                    }),
                    THEME,
                    EditorView.theme({
                        '.cm-content, .cm-gutter': { minHeight: `${minLines * lineHeight}px` },
                        '.cm-scroller': { maxHeight: `${maxLines * lineHeight + 12}px`, overflow: 'auto' },
                    }),
                    attrs.current.of(contentAttrs()),
                    ph.current.of(placeholder ? placeholderExt(placeholder) : []),
                ],
            }),
        });
        view.current = v;
        return () => {
            v.destroy();
            view.current = null;
        };
        // The editor is made once; value, attributes and placeholder are kept in step below.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    // The text from outside (a saved query opened, a name inserted): replaced without losing undo history.
    useEffect(() => {
        const v = view.current;
        if (!v) return;
        if (v.state.doc.toString() === value) {
            emitted.current = [value];            // in step: only what is typed from here on can be caught up with
            return;
        }
        if (emitted.current.includes(value)) return;
        emitted.current = [value];
        v.dispatch({ changes: { from: 0, to: v.state.doc.length, insert: value },
            selection: EditorSelection.cursor(Math.min(v.state.selection.main.head, value.length)) });
    }, [value]);

    useEffect(() => {
        view.current?.dispatch({ effects: attrs.current.reconfigure(contentAttrs()) });
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [id, labelledBy, ariaLabel, describedBy, invalid]);

    useEffect(() => {
        view.current?.dispatch({ effects: ph.current.reconfigure(placeholder ? placeholderExt(placeholder) : []) });
    }, [placeholder]);

    useImperativeHandle(ref, () => ({
        selection: () => {
            const m = view.current?.state.selection.main;
            return m ? { from: m.from, to: m.to } : { from: 0, to: 0 };
        },
        focus: () => view.current?.focus(),
        insert: (text: string) => {
            const v = view.current;
            if (!v) return;
            v.dispatch(v.state.replaceSelection(text));
            v.focus();
        },
        select: (from: number, to: number) => {
            const v = view.current;
            if (!v) return;
            const end = Math.min(to, v.state.doc.length);
            v.dispatch({ selection: EditorSelection.range(Math.min(from, end), end), scrollIntoView: true });
            v.focus();
        },
        replace: (from: number, to: number, text: string) => {
            const v = view.current;
            if (!v) return;
            const end = Math.min(to, v.state.doc.length);
            const start = Math.min(from, end);
            v.dispatch({ changes: { from: start, to: end, insert: text },
                selection: EditorSelection.range(start, start + text.length), scrollIntoView: true });
            v.focus();
        },
    }), []);

    return <div ref={host} className="cairn-sql-editor" data-testid="sql-editor" />;
});
