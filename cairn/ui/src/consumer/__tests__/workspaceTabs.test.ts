import type { SavedQuery } from '../api/types';
import { closeTab, isDirty, maySave, openSaved, refreshed, remember, restore, saved, untitled, untitledName } from '../sql/workspaceTabs';

const Q = (id: string, sql = 'SELECT 1', role: SavedQuery['role'] = 'OWNER'): SavedQuery => ({
    id, owner: 'urn:li:corpuser:ana', role, name: `Q ${id}`, description: '', connection: 'trino', sql, shared: false,
    chart: { type: 'table' }, created_ms: 1, updated_ms: 1,
});

describe('the editor\'s open files (L163)', () => {
    it('names drafts, opens a file once, and closes to the neighbour', () => {
        const a = untitled([]);
        expect(a.name).toBe('Untitled 1');
        expect(untitledName([a, { ...untitled([a]), name: 'Untitled 3' }])).toBe('Untitled 2');
        const one = openSaved([a], Q('q1'));
        const again = openSaved(one.tabs, Q('q1'));
        expect(again.tabs).toHaveLength(2);
        expect(again.key).toBe(one.key);                         // already open: that tab
        const b = untitled(one.tabs);
        const three = [...one.tabs, b];
        expect(closeTab(three, one.key, one.key).active).toBe(b.key);       // to its right
        expect(closeTab(three, b.key, b.key).active).toBe(one.key);         // the last: to its left
        expect(closeTab(three, a.key, b.key).active).toBe(b.key);           // another tab: the shown one stays
    });

    it('knows what is unsaved, who may save, and keeps typing done while saving unsaved', () => {
        const t = openSaved([], Q('q1', 'SELECT 1')).tabs[0];
        expect(isDirty(t)).toBe(false);
        expect(isDirty({ ...t, sql: 'SELECT 2' })).toBe(true);
        expect(isDirty(untitled([]))).toBe(false);
        expect(isDirty(untitled([], '  '))).toBe(false);
        expect(isDirty(untitled([], 'SELECT 1'))).toBe(true);
        expect(maySave(openSaved([], Q('q2', 'x', 'VIEWER')).tabs[0])).toBe(false);
        expect(maySave(openSaved([], Q('q3', 'x', 'EDITOR')).tabs[0])).toBe(true);
        // Saved "SELECT 2" while the editor already holds "SELECT 22": still unsaved.
        const typed = [{ ...t, sql: 'SELECT 22' }];
        const after = saved(typed, t.key, Q('q1', 'SELECT 2'), 'SELECT 2');
        expect(after[0].savedSql).toBe('SELECT 2');
        expect(isDirty(after[0])).toBe(true);
        // A draft saved becomes the saved query's tab.
        const d = untitled([], 'SELECT 9');
        expect(saved([d], d.key, Q('q9', 'SELECT 9'), 'SELECT 9')[0]).toMatchObject({ savedId: 'q9', name: 'Q q9', role: 'OWNER' });
    });

    it('keeps open tabs on this device: drafts and unsaved text, never a saved file\'s text twice', () => {
        window.localStorage.clear();
        const f = openSaved([], Q('q1', 'SELECT 1')).tabs[0];
        const edited = { ...openSaved([], Q('q2', 'SELECT 2')).tabs[0], sql: 'SELECT 22' };
        const draft = untitled([f, edited], 'SELECT 3');
        remember('ana', [f, edited, draft], edited.key);
        const kept = JSON.parse(window.localStorage.getItem('cairn.sql.tabs:ana')!);
        expect(kept.tabs.map((t: { sql?: string }) => t.sql)).toEqual([undefined, 'SELECT 22', 'SELECT 3']);
        // Back again: q1 as saved now, q2 with its unsaved text; q2 deleted since: its text as a draft.
        const back = restore('ana', [Q('q1', 'SELECT 1 -- newer'), Q('q2', 'SELECT 2')]);
        expect(back.tabs.map((t) => [t.name, t.sql, isDirty(t)])).toEqual([
            ['Q q1', 'SELECT 1 -- newer', false], ['Q q2', 'SELECT 22', true], ['Untitled 1', 'SELECT 3', true]]);
        expect(back.active).toBe(edited.key);
        const gone = restore('ana', [Q('q1')]);
        expect(gone.tabs.map((t) => [t.name, t.savedId])).toEqual([['Q q1', 'q1'], ['Q q2 (unsaved)', undefined], ['Untitled 1', undefined]]);
        expect(restore('bob', []).tabs).toEqual([]);                  // another person's tabs are not mine
        window.localStorage.setItem('cairn.sql.tabs:ana', '{not json');
        expect(restore('ana', []).tabs).toEqual([]);
    });
});

describe('open tabs follow Cairn (L163)', () => {
    const q = (over: Partial<SavedQuery> = {}): SavedQuery => ({ id: 'q1', name: 'Revenue', sql: 'SELECT 1', connection: 'wh',
        description: '', shared: false, owner: 'urn:li:corpuser:ana', role: 'OWNER', ...over } as SavedQuery);

    it('takes a rename from the file tree, keeps unsaved text, and turns a deleted query into a draft', () => {
        const open = openSaved([], q()).tabs.map((t) => ({ ...t, sql: 'SELECT 2' }));        // typed, not saved yet
        expect(refreshed(open, [q()])).toBe(open);                                          // nothing changed: same list
        const renamed = refreshed(open, [q({ name: 'Revenue (EU)', shared: true })]);
        expect(renamed[0]).toMatchObject({ name: 'Revenue (EU)', shared: true, sql: 'SELECT 2', savedSql: 'SELECT 1' });
        expect(refreshed(open, [])).toBe(open);                      // not listed before: just saved, not deleted
        const gone = refreshed(open, [], new Set(['q1']));
        expect(gone[0].savedId).toBeUndefined();
        expect(gone[0]).toMatchObject({ name: 'Revenue (deleted)', sql: 'SELECT 2' });
        expect(isDirty(gone[0])).toBe(true);
    });
});
