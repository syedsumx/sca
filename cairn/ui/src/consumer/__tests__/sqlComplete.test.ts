import { describe, expect, it } from 'vitest';

import type { SchemaTable } from '../api/types';
import { accept, blank, complete, quoteIdent, sources } from '../sql/complete';

const T: SchemaTable[] = [
    { urn: 'u1', name: 'sales.orders', columns: [{ name: 'id', type: 'integer' }, { name: 'region', type: 'varchar' },
        { name: 'amount', type: 'double' }, { name: 'customer_id', type: 'bigint' }] },
    { urn: 'u2', name: 'sales.customers', columns: [{ name: 'id', type: 'bigint' }, { name: 'email', type: 'varchar' },
        { name: 'Order Count', type: 'integer' }] },
    { urn: 'u3', name: 'hr.people', columns: [{ name: 'name', type: 'varchar' }] },
];

const at = (sql: string) => {
    const cursor = sql.indexOf('|');
    const text = sql.replace('|', '');
    return { text, c: complete(text, cursor, T) };
};
const labels = (sql: string) => at(sql).c.items.map((s) => s.label);

describe('SQL autocomplete', () => {
    it('offers tables after FROM and JOIN, and after a comma in FROM', () => {
        expect(labels('SELECT * FROM |')).toEqual(expect.arrayContaining(['sales.orders', 'sales.customers', 'hr.people', 'sales', 'hr']));
        expect(labels('SELECT * FROM sales.orders o JOIN cu|')[0]).toBe('sales.customers');
        expect(labels('SELECT * FROM sales.orders, |')).toContain('hr.people');
        expect(labels('select * from ord|')[0]).toBe('sales.orders');
    });

    it('offers a schema\'s tables after "schema."', () => {
        expect(labels('SELECT * FROM sales.|')).toEqual(['customers', 'orders']);
        expect(labels('SELECT * FROM sales.or|')).toEqual(['orders']);
    });

    it("offers a table's or alias's columns after its name and a dot", () => {
        expect(labels('SELECT o.| FROM sales.orders o')).toEqual(['amount', 'customer_id', 'id', 'region']);
        expect(labels('SELECT orders.re| FROM sales.orders')).toEqual(['region']);
        expect(labels('SELECT c.| FROM sales.orders o JOIN sales.customers AS c ON o.customer_id = c.id')).toEqual(
            ['email', 'id', 'Order Count']);
    });

    it('offers the columns of the tables read, qualified when two share a name', () => {
        const items = at('SELECT re| FROM sales.orders').c.items;
        expect(items[0]).toMatchObject({ label: 'region', insert: 'region', kind: 'column' });
        const both = labels('SELECT | FROM sales.orders o JOIN sales.customers c ON o.customer_id = c.id');
        expect(both).toEqual(expect.arrayContaining(['o.id', 'c.id', 'region', 'email']));
        expect(both).not.toContain('id');
        expect(labels('SELECT region, coun| FROM sales.orders')).toEqual(expect.arrayContaining(['count', 'count_if']));
        expect(labels('SELECT * FROM sales.orders WHE|')).toContain('WHERE');
    });

    it('offers nothing inside strings and comments', () => {
        expect(labels("SELECT * FROM sales.orders WHERE region = 'EU|'")).toEqual([]);
        expect(labels('SELECT * FROM sales.orders -- reg|')).toEqual([]);
        expect(labels('SELECT /* reg| */ 1 FROM sales.orders')).toEqual([]);
        expect(blank("a 'x' b").text).toBe('a     b');
    });

    it('quotes names that need it and puts the suggestion in place of the word', () => {
        expect(quoteIdent('Order Count')).toBe('"Order Count"');
        expect(quoteIdent('select')).toBe('"select"');
        expect(quoteIdent('region')).toBe('region');
        const { text, c } = at('SELECT c.Ord| FROM sales.customers c');
        const s = c.items.find((i) => i.label === 'Order Count')!;
        expect(accept(text, c, s)).toEqual({ sql: 'SELECT c."Order Count" FROM sales.customers c', cursor: 22 });
        const t = at('SELECT * FROM sal|');
        expect(accept(t.text, t.c, t.c.items[0]).sql).toBe('SELECT * FROM sales.customers');
    });

    it('reads aliases and leaves out keywords taken for aliases', () => {
        expect(sources('SELECT * FROM sales.orders o JOIN "sales"."customers" AS c ON true', T).map((s) => [s.table.name, s.alias]))
            .toEqual([['sales.orders', 'o'], ['sales.customers', 'c']]);
        expect(sources('SELECT * FROM sales.orders WHERE x', T)[0].alias).toBeUndefined();
        expect(sources('SELECT * FROM nope.nothing', T)).toEqual([]);   // tables the person may not query are unknown
    });
});
