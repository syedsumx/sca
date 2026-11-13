import { splitStatements, statementAt, statementTitle, toRun } from '../sql/statements';

describe('a SQL file as statements (L163)', () => {
    it('splits on ; only outside strings, quoted names and comments', () => {
        const sql = [
            "SELECT 'a;b', \"x;y\" FROM t;",
            '-- a comment; not a statement',
            "SELECT 'it''s; fine' /* ; */ FROM u ;",
            '',
            ';;',
            '/* only a comment */;',
            'SELECT `q;r` FROM v',
        ].join('\n');
        const s = splitStatements(sql);
        expect(s.map((x) => x.text)).toEqual([
            "SELECT 'a;b', \"x;y\" FROM t",
            "-- a comment; not a statement\nSELECT 'it''s; fine' /* ; */ FROM u",
            'SELECT `q;r` FROM v',
        ]);
        for (const x of s) expect(sql.slice(x.from, x.to)).toBe(x.text);         // ranges point at the text
    });

    it('an unclosed string or comment runs to the end and does not split', () => {
        expect(splitStatements("SELECT 'open; still open").map((s) => s.text)).toEqual(["SELECT 'open; still open"]);
        expect(splitStatements('SELECT 1 /* ; ').map((s) => s.text)).toEqual(['SELECT 1 /* ;']);
        expect(splitStatements('  \n -- nothing\n ')).toEqual([]);
    });

    it('the statement at the cursor, the selection, or all', () => {
        const sql = 'SELECT 1;\nSELECT 2;\n\nSELECT 3';
        const all = splitStatements(sql);
        expect(statementAt(all, 0)?.text).toBe('SELECT 1');
        expect(statementAt(all, sql.indexOf('2'))?.text).toBe('SELECT 2');
        expect(statementAt(all, sql.indexOf('2') + 2)?.text).toBe('SELECT 2');        // just after its ;
        expect(statementAt(all, sql.length)?.text).toBe('SELECT 3');
        expect(statementAt([], 3)).toBeNull();
        expect(toRun(sql, { from: sql.indexOf('2'), to: sql.indexOf('2') }, 'current').map((s) => s.text)).toEqual(['SELECT 2']);
        const sel = { from: sql.indexOf('SELECT 2'), to: sql.length };
        const picked = toRun(sql, sel, 'current');
        expect(picked.map((s) => s.text)).toEqual(['SELECT 2', 'SELECT 3']);
        expect(picked.map((s) => sql.slice(s.from, s.to))).toEqual(['SELECT 2', 'SELECT 3']);   // file positions
        expect(toRun(sql, { from: 0, to: 0 }, 'all')).toHaveLength(3);
    });

    it('titles a result tab with the statement on one line', () => {
        expect(statementTitle('-- why\nSELECT region,\n   count(*)\nFROM orders')).toBe('SELECT region, count(*) FROM orders');
        expect(statementTitle('SELECT ' + 'x, '.repeat(30), 20)).toHaveLength(20);
    });
});
