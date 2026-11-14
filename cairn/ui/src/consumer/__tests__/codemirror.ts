/**
 * Driving the SQL editor (CodeMirror, L163) from jsdom tests: jsdom does no layout, so the measuring calls get
 * empty answers, and text is put in through the editor's own transactions, as typing does.
 */
import { EditorSelection } from '@codemirror/state';
import { EditorView } from '@codemirror/view';

export function installLayoutStubs(): void {
    const empty = () => ({ length: 0, item: () => null, [Symbol.iterator]: [][Symbol.iterator] }) as unknown as DOMRectList;
    const rect = () => ({ x: 0, y: 0, top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0, toJSON: () => ({}) }) as DOMRect;
    if (!Range.prototype.getClientRects) Range.prototype.getClientRects = empty;
    if (!Range.prototype.getBoundingClientRect) Range.prototype.getBoundingClientRect = rect;
}

/** The editor the element (its labelled text area, or anything inside it) belongs to. */
export function editorOf(el: Element): EditorView {
    const v = EditorView.findFromDOM(el as HTMLElement);
    if (!v) throw new Error('not inside a SQL editor');
    return v;
}

/** The editor's text. */
export function sqlOf(el: Element): string {
    return editorOf(el).state.doc.toString();
}

/** Types ``text`` in place of the editor's text (one transaction, as a paste), with the cursor at ``cursor`` (the end). */
export function typeSql(el: Element, text: string, cursor?: number): void {
    const v = editorOf(el);
    v.dispatch({ changes: { from: 0, to: v.state.doc.length, insert: text },
        selection: EditorSelection.cursor(cursor ?? text.length), userEvent: 'input.type' });
}

/** Selects ``from``-``to`` (a cursor when they are equal). */
export function selectSql(el: Element, from: number, to = from): void {
    editorOf(el).dispatch({ selection: EditorSelection.range(from, to) });
}
