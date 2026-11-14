/**
 * Cairn Ontology's explorer in the standalone demo (no backend): a compiled ontology of about forty business terms
 * across four domains (Sales, Customer, Supply Chain, Finance), relationships known from keys, from the query log
 * and inferred, two bridge tables, proposals waiting for review, a compile that progresses, and the diff of the last
 * run. All data is fictional.
 */

type Json = any; // eslint-disable-line @typescript-eslint/no-explicit-any
type Handler = (body: Json, q: URLSearchParams, params: string[]) => unknown;

const ds = (platform: string, name: string) => `urn:li:dataset:(urn:li:dataPlatform:${platform},${name},PROD)`;

interface Spec {
    domain: string;
    schema: string;
    table: string;
    urn?: string;
    term: string | null;
    synonyms?: string[];
    description: string;
    /** `name:type`, `name:type:FORMAT` for a string date, `name>table.column` for a foreign key. */
    columns: string[];
    owner: string;
}

const DOMAINS: Record<string, string> = {
    Sales: 'urn:li:domain:sales',
    Customer: 'urn:li:domain:marketing',
    'Supply Chain': 'urn:li:domain:supply-chain',
    Finance: 'urn:li:domain:finance',
};

const RAJ = 'urn:li:corpuser:raj';
const MEI = 'urn:li:corpuser:mei';
const TOM = 'urn:li:corpuser:tom';
const ALICE = 'urn:li:corpuser:alice';

const SPECS: Spec[] = [
    // Sales
    { domain: 'Sales', schema: 'core', table: 'orders', urn: ds('snowflake', 'analytics.core.orders'), term: 'Order', synonyms: ['PO', 'purchase order'],
      description: 'A customer order placed through any sales channel.', owner: RAJ,
      columns: ['order_id:varchar', 'customer_id>customers.id', 'order_ts:timestamp', 'amount:number', 'discount:number', 'region:varchar', 'channel_id>channels.id', 'rep_id>sales_reps.id'] },
    { domain: 'Sales', schema: 'sales', table: 'order_lines', term: 'Order Line', synonyms: ['line item'], description: 'One product on an order, with its quantity and price.', owner: RAJ,
      columns: ['line_id:bigint', 'order_id>orders.order_id', 'product_id>products.id', 'qty:integer', 'unit_price:decimal'] },
    { domain: 'Sales', schema: 'sales', table: 'returns', term: 'Return', synonyms: ['RMA'], description: 'Goods a customer sent back.', owner: RAJ,
      columns: ['return_id:bigint', 'order_id>orders.order_id', 'reason:varchar', 'returned_on:varchar:DD/MM/YYYY'] },
    { domain: 'Sales', schema: 'sales', table: 'promotions', term: 'Promotion', description: 'A time-boxed offer.', owner: MEI,
      columns: ['id:bigint', 'name:varchar', 'starts:varchar:YYYY-MM-DD', 'ends:varchar:YYYY-MM-DD'] },
    { domain: 'Sales', schema: 'sales', table: 'channels', term: 'Sales Channel', description: 'Web, store, marketplace or partner.', owner: RAJ,
      columns: ['id:bigint', 'name:varchar'] },
    { domain: 'Sales', schema: 'sales', table: 'quotes', term: 'Quote', synonyms: ['proposal'], description: 'Prices offered to an account before it orders.', owner: RAJ,
      columns: ['id:bigint', 'account_id>accounts.id', 'valid_until:varchar:YYYYMMDD', 'total:decimal'] },
    { domain: 'Sales', schema: 'sales', table: 'price_lists', term: 'Price List', description: 'Prices per product and currency.', owner: TOM,
      columns: ['id:bigint', 'product_id>products.id', 'currency_code>currencies.code', 'price:decimal'] },
    { domain: 'Sales', schema: 'sales', table: 'sales_reps', term: 'Sales Representative', synonyms: ['rep', 'account executive'], description: 'A person who sells.', owner: RAJ,
      columns: ['id:bigint', 'name:varchar', 'territory:varchar'] },
    { domain: 'Sales', schema: 'sales', table: 'carts', term: 'Shopping Cart', synonyms: ['basket'], description: 'Products a shopper has not bought yet.', owner: MEI,
      columns: ['id:bigint', 'customer_id>customers.id', 'created:varchar:YYYY-MM-DD HH:mm:ss'] },
    { domain: 'Sales', schema: 'sales', table: 'order_promotions', term: null, description: 'Which promotions applied to which orders.', owner: RAJ,
      columns: ['order_id>orders.order_id', 'promotion_id>promotions.id'] },
    // Customer
    { domain: 'Customer', schema: 'core', table: 'customers', urn: ds('snowflake', 'analytics.core.customers'), term: 'Customer', synonyms: ['client', 'buyer'],
      description: 'A person or company that buys from us.', owner: MEI,
      columns: ['id:varchar', 'email:varchar', 'phone:varchar', 'status:varchar', 'signup_date:date', 'segment_id>segments.id'] },
    { domain: 'Customer', schema: 'crm', table: 'accounts', term: 'Account', synonyms: ['B2B account'], description: 'A business customer with contracts.', owner: MEI,
      columns: ['id:bigint', 'name:varchar', 'customer_id>customers.id', 'tier:varchar'] },
    { domain: 'Customer', schema: 'crm', table: 'contacts', term: 'Contact', description: 'A person at an account.', owner: MEI,
      columns: ['id:bigint', 'account_id>accounts.id', 'role:varchar'] },
    { domain: 'Customer', schema: 'crm', table: 'segments', term: 'Customer Segment', synonyms: ['cohort'], description: 'A group of customers marketed to alike.', owner: MEI,
      columns: ['id:bigint', 'name:varchar'] },
    { domain: 'Customer', schema: 'crm', table: 'addresses', term: 'Address', description: 'Where a customer receives goods and invoices.', owner: MEI,
      columns: ['id:bigint', 'customer_id>customers.id', 'country:varchar', 'postcode:varchar'] },
    { domain: 'Customer', schema: 'crm', table: 'subscriptions', term: 'Subscription', synonyms: ['plan'], description: 'A recurring plan a customer pays for.', owner: MEI,
      columns: ['id:bigint', 'customer_id>customers.id', 'plan:varchar', 'renews_on:varchar:MM/DD/YYYY'] },
    { domain: 'Customer', schema: 'crm', table: 'support_tickets', term: 'Support Ticket', synonyms: ['case'], description: 'A customer asking for help.', owner: TOM,
      columns: ['id:bigint', 'customer_id>customers.id', 'opened:timestamp', 'priority:varchar'] },
    { domain: 'Customer', schema: 'crm', table: 'loyalty_members', term: 'Loyalty Member', description: 'A customer in the loyalty programme.', owner: MEI,
      columns: ['id:bigint', 'customer_id>customers.id', 'points:integer'] },
    { domain: 'Customer', schema: 'crm', table: 'campaigns', term: 'Campaign', description: 'A marketing campaign.', owner: MEI,
      columns: ['id:bigint', 'name:varchar', 'promotion_id>promotions.id', 'budget:decimal'] },
    { domain: 'Customer', schema: 'crm', table: 'campaign_touches', term: 'Campaign Touch', description: 'A customer reached by a campaign.', owner: MEI,
      columns: ['id:bigint', 'campaign_id>campaigns.id', 'customer_id>customers.id', 'touched:varchar:YYYY-MM-DD'] },
    // Supply chain
    { domain: 'Supply Chain', schema: 'supply', table: 'products', term: 'Product', synonyms: ['SKU', 'item'], description: 'Something we sell.', owner: TOM,
      columns: ['id:bigint', 'name:varchar', 'brand:varchar', 'launched:varchar:YYYY-MM'] },
    { domain: 'Supply Chain', schema: 'supply', table: 'categories', term: 'Product Category', description: 'A shelf in the catalogue.', owner: TOM,
      columns: ['id:bigint', 'label:varchar'] },
    { domain: 'Supply Chain', schema: 'supply', table: 'product_categories', term: null, description: 'Which categories each product is in.', owner: TOM,
      columns: ['product_id>products.id', 'category_id>categories.id'] },
    { domain: 'Supply Chain', schema: 'supply', table: 'suppliers', term: 'Supplier', synonyms: ['vendor'], description: 'A company we buy from.', owner: TOM,
      columns: ['id:bigint', 'name:varchar', 'country:varchar'] },
    { domain: 'Supply Chain', schema: 'supply', table: 'product_suppliers', term: null, description: 'Which suppliers can provide each product.', owner: TOM,
      columns: ['product_id>products.id', 'supplier_id>suppliers.id'] },
    { domain: 'Supply Chain', schema: 'supply', table: 'warehouses', term: 'Warehouse', synonyms: ['DC', 'distribution centre'], description: 'Where stock is kept.', owner: TOM,
      columns: ['id:bigint', 'city:varchar', 'capacity:integer'] },
    { domain: 'Supply Chain', schema: 'public', table: 'inventory_levels', urn: ds('postgres', 'wms.public.inventory_levels'), term: 'Inventory Level', synonyms: ['stock'],
      description: 'How many of a product a warehouse holds.', owner: TOM,
      columns: ['product_id>products.id', 'warehouse_id>warehouses.id', 'on_hand:integer', 'as_of:varchar:YYYY-MM-DD'] },
    { domain: 'Supply Chain', schema: 'supply', table: 'shipments', urn: ds('snowflake', 'analytics.supply.shipments'), term: 'Shipment', synonyms: ['delivery'],
      description: 'Goods on their way to a customer.', owner: TOM,
      columns: ['id:bigint', 'order_id>orders.order_id', 'carrier_id>carriers.id', 'warehouse_id>warehouses.id', 'promised_at:timestamp', 'delivered_at:timestamp'] },
    { domain: 'Supply Chain', schema: 'supply', table: 'carriers', term: 'Carrier', description: 'A company that delivers shipments.', owner: TOM,
      columns: ['id:bigint', 'name:varchar'] },
    { domain: 'Supply Chain', schema: 'supply', table: 'supplier_orders', term: 'Supplier Order', description: 'Stock we ordered from a supplier.', owner: TOM,
      columns: ['id:bigint', 'supplier_id>suppliers.id', 'product_id>products.id', 'ordered:varchar:DD.MM.YYYY'] },
    { domain: 'Supply Chain', schema: 'supply', table: 'stock_movements', term: 'Stock Movement', description: 'Stock moved between warehouses.', owner: TOM,
      columns: ['id:bigint', 'product_id>products.id', 'from_warehouse>warehouses.id', 'to_warehouse>warehouses.id', 'qty:integer'] },
    // Finance
    { domain: 'Finance', schema: 'public', table: 'invoices', urn: ds('postgres', 'billing.public.invoices'), term: 'Invoice', synonyms: ['bill'],
      description: 'What a customer owes for an order.', owner: ALICE,
      columns: ['invoice_id:uuid', 'order_id>orders.order_id', 'total:numeric', 'due:varchar:YYYY-MM-DD'] },
    { domain: 'Finance', schema: 'finance', table: 'payments', term: 'Payment', description: 'Money received against an invoice.', owner: ALICE,
      columns: ['id:bigint', 'invoice_id>invoices.invoice_id', 'amount:decimal', 'method:varchar', 'paid_on:varchar:YYYY-MM-DD'] },
    { domain: 'Finance', schema: 'finance', table: 'refunds', term: 'Refund', description: 'Money paid back to a customer.', owner: ALICE,
      columns: ['id:bigint', 'payment_id>payments.id', 'return_id>returns.return_id', 'amount:decimal'] },
    { domain: 'Finance', schema: 'finance', table: 'ledger_entries', term: 'Ledger Entry', synonyms: ['GL entry', 'journal line'], description: 'One line of the general ledger.', owner: ALICE,
      columns: ['id:bigint', 'cost_center_id>cost_centers.id', 'account_code:varchar', 'amount:decimal', 'posted:varchar:YYYYMMDD'] },
    { domain: 'Finance', schema: 'finance', table: 'currencies', term: 'Currency', description: 'A currency we trade in.', owner: ALICE,
      columns: ['code:varchar', 'name:varchar'] },
    { domain: 'Finance', schema: 'finance', table: 'fx_rates', term: 'Exchange Rate', synonyms: ['FX rate'], description: 'The rate between two currencies on a day.', owner: TOM,
      columns: ['currency_code>currencies.code', 'rate:decimal', 'day:varchar:YYYY-MM-DD'] },
    { domain: 'Finance', schema: 'finance', table: 'cost_centers', term: 'Cost Center', description: 'Where costs are booked.', owner: ALICE,
      columns: ['id:bigint', 'name:varchar'] },
    { domain: 'Finance', schema: 'finance', table: 'budgets', term: 'Budget', description: 'Money planned per cost center and month.', owner: ALICE,
      columns: ['id:bigint', 'cost_center_id>cost_centers.id', 'month:varchar:YYYY-MM', 'amount:decimal'] },
    { domain: 'Finance', schema: 'finance', table: 'tax_rates', term: 'Tax Rate', synonyms: ['VAT rate'], description: 'Sales tax by country.', owner: ALICE,
      columns: ['country:varchar', 'rate:decimal'] },
    { domain: 'Finance', schema: 'finance', table: 'revenue_daily', urn: ds('snowflake', 'analytics.finance.revenue_daily'), term: 'Daily Revenue', synonyms: ['net revenue'],
      description: 'Net revenue per day and region.', owner: ALICE,
      columns: ['day:date', 'region:varchar', 'net_revenue:number', 'orders:number'] },
];

const RUN = 'run_d41c5a7e93b20f18';
const PREV = 'run_9e02b77c1f4a6d35';
const urnOf = (s: Spec) => s.urn ?? ds('trino', `lakehouse.${s.schema}.${s.table}`);
const byTable = new Map(SPECS.map((s) => [s.table, s]));
const slug = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, '_');
const label = (c: string) => c.replace(/_/g, ' ').replace(/\b\w/g, (m) => m.toUpperCase());
// A stable pseudo-confidence per name, between 0.55 and 0.97.
const conf = (s: string) => {
    let h = 7;
    for (const c of s) h = (h * 31 + c.charCodeAt(0)) % 997;
    return Math.round((0.55 + (h % 43) / 100) * 100) / 100;
};
const isBridge = (s: Spec) => s.term === null;
const QUERY_JOINS = new Set(['orders.customer_id', 'order_lines.product_id', 'shipments.order_id', 'payments.invoice_id', 'campaign_touches.customer_id', 'inventory_levels.product_id']);
const INFERRED = new Set(['carts.customer_id', 'campaigns.promotion_id', 'refunds.return_id', 'stock_movements.from_warehouse', 'stock_movements.to_warehouse', 'quotes.account_id']);
// A relationship the last run found, and the one it no longer finds (the diff).
const NEW_REL = 'refunds.return_id';

export function buildOntology() {
    const terms: Json[] = [];
    const attributes: Json[] = [];
    const relationships: Json[] = [];
    const bridges: Json[] = [];
    for (const s of SPECS) {
        const urn = urnOf(s);
        const termId = s.term ? `term_${slug(s.table)}` : null;
        const fks = s.columns.filter((c) => c.includes('>'));
        for (const c of fks) {
            const [col, ref] = c.split('>');
            const [tt, tc] = ref.split('.');
            const target = byTable.get(tt) as Spec;
            const key = `${s.table}.${col}`;
            const prov = INFERRED.has(key) ? ['model'] : QUERY_JOINS.has(key) ? ['catalog_fk', 'observed_in_queries'] : isBridge(s) ? ['inferred'] : ['catalog_fk'];
            const queries = QUERY_JOINS.has(key) ? 3 + (conf(key) * 40 | 0) : 0;
            relationships.push({
                id: `rel_${slug(key)}`, source_table: urn, source_column: col, target_table: urnOf(target), target_column: tc,
                cardinality: 'many_to_one', confidence: INFERRED.has(key) ? conf(key) : QUERY_JOINS.has(key) ? 0.98 : 0.95,
                join_default: !(s.table === 'stock_movements' && col === 'to_warehouse'),
                provenance: prov.map((p) => ({ source: p, detail: p === 'model' ? `${col} names a ${target.term ?? tt}` : p === 'catalog_fk' ? `declared key ${s.table}.${col}` : p === 'observed_in_queries' ? `joined in ${queries} queries` : 'every column is a foreign key', confidence: p === 'model' ? conf(key) : 1, run_id: RUN })),
                evidence: { queries, executions: queries * 17, query_ids: queries ? [`q_${slug(key).slice(0, 6)}1`, `q_${slug(key).slice(0, 6)}2`] : [] },
                rationale: INFERRED.has(key) ? `Values of ${col} match ${tt}.${tc}; the model judged it a reference.` : '',
            });
        }
        if (isBridge(s)) {
            bridges.push({ id: `brg_${slug(s.table)}`, table_id: urn, table_name: `${s.schema}.${s.table}`, metric_id: `met_${slug(s.table)}`,
                fk_pairs: fks.map((c) => { const [col, ref] = c.split('>'); const [tt, tc] = ref.split('.'); return { source_column: col, target_table: urnOf(byTable.get(tt) as Spec), target_column: tc }; }),
                provenance: [{ source: 'inferred', detail: 'every column is a foreign key', confidence: 0.9, run_id: RUN }] });
            continue;
        }
        const own = s.columns.filter((c) => !c.includes('>'));
        terms.push({ id: termId, name: s.term, description: s.description, synonyms: s.synonyms ?? [], table_id: urn, table_name: `${s.schema}.${s.table}`,
            proposed_name: s.term, attribute_ids: own.map((c) => `attr_${slug(s.table)}_${c.split(':')[0]}`),
            provenance: [{ source: 'model', detail: `business Term for table ${s.schema}.${s.table}`, confidence: conf(s.table), run_id: RUN }] });
        for (const c of own) {
            const [col, type, format] = c.split(':');
            attributes.push({ id: `attr_${slug(s.table)}_${col}`, term_id: termId, term_name: s.term, table_id: urn, column: col, name: label(col), datatype: type,
                description: `The ${col.replace(/_/g, ' ')} of the ${(s.term as string).toLowerCase()}.`, format: format ?? null, is_unique: col === 'id' || col.endsWith('_id'),
                deprecated: false, provenance: [{ source: 'catalog', detail: `column ${s.schema}.${s.table}.${col}`, confidence: 1, run_id: RUN }] });
        }
    }
    const t = (table: string) => `term_${table}`;
    const metrics: Json[] = [
        { id: 'met_net_revenue', term_id: t('orders'), term_name: 'Order', name: 'Net Revenue', description: 'Order amount less discounts.', expression: 'sum(o.amount - o.discount)', sql: 'SELECT sum(o.amount - o.discount) FROM core.orders o', source: 'sql', tables: [urnOf(byTable.get('orders') as Spec)], usage: 212, source_query_ids: ['q_rev1'] },
        { id: 'met_late_shipments', term_id: t('shipments'), term_name: 'Shipment', name: 'Late Shipments', description: 'Delivered after the promised time.', expression: 'count_if(s.delivered_at > s.promised_at)', sql: 'SELECT count_if(s.delivered_at > s.promised_at) FROM supply.shipments s', source: 'sql', tables: [urnOf(byTable.get('shipments') as Spec)], usage: 48, source_query_ids: [] },
        { id: 'met_outstanding', term_id: t('invoices'), term_name: 'Invoice', name: 'Outstanding Balance', description: 'Invoiced and not yet paid.', expression: 'sum(i.total) - sum(p.amount)', sql: 'SELECT sum(i.total) - sum(p.amount) FROM public.invoices i LEFT JOIN finance.payments p ON p.invoice_id = i.invoice_id', source: 'sql', tables: [urnOf(byTable.get('invoices') as Spec), urnOf(byTable.get('payments') as Spec)], usage: 31, source_query_ids: [] },
        { id: 'met_product_categories', term_id: t('products'), term_name: 'Product', name: 'Products by Category', description: 'Which categories each product is in.', expression: 'SELECT * FROM supply.product_categories b JOIN supply.products t0 ON b.product_id = t0.id JOIN supply.categories t1 ON b.category_id = t1.id', sql: 'SELECT * FROM supply.product_categories b JOIN supply.products t0 ON b.product_id = t0.id JOIN supply.categories t1 ON b.category_id = t1.id', source: 'bridgeTable', tables: [urnOf(byTable.get('product_categories') as Spec), urnOf(byTable.get('products') as Spec), urnOf(byTable.get('categories') as Spec)], usage: 0, source_query_ids: [] },
    ];
    return { run_id: RUN, version: 7, terms, attributes, relationships, metrics, bridges };
}

/** What the explorer's API answers in the demo; decisions and runs change it for the session. */
export function installOntologyDemo(on: (method: string, pattern: string, h: Handler) => void, fail: (status: number, msg: string) => never, me: string) {
    const o = buildOntology();
    const specOf = new Map(SPECS.map((s) => [urnOf(s), s]));
    const status = new Map<string, string>();         // element id → accepted | proposed | rejected
    const proposals: Json[] = [];
    // The well-known core is accepted; newer and inferred elements wait for review.
    for (const term of o.terms) {
        const s = specOf.get(term.table_id) as Spec;
        const pending = ['Sales', 'Customer'].includes(s.domain) ? ['returns', 'quotes', 'carts', 'campaign_touches', 'loyalty_members', 'subscriptions'].includes(s.table)
            : ['stock_movements', 'supplier_orders', 'budgets', 'tax_rates', 'fx_rates'].includes(s.table);
        status.set(term.id, pending ? 'proposed' : 'accepted');
        if (pending) {
            proposals.push({ id: `fnd_${term.id.slice(5)}`, type: 'TERM_SUGGESTION', kind: 'term', status: 'OPEN', confidence: term.provenance[0].confidence,
                title: `Add glossary term “${term.name}”`, summary_md: `${term.description}\n\nCompiled from \`${term.table_name}\` (ontology run 7).`,
                entities: [term.table_id], element_id: term.id, version: 7, proposed_changes: [] });
        }
    }
    for (const r of o.relationships) {
        const inferred = r.provenance.some((p: Json) => p.source === 'model');
        status.set(r.id, inferred ? 'proposed' : 'accepted');
        if (inferred) {
            const s = specOf.get(r.source_table) as Spec;
            const t = specOf.get(r.target_table) as Spec;
            proposals.push({ id: `fnd_${r.id.slice(4)}`, type: 'JOIN_RELATIONSHIP', kind: 'relationship', status: 'OPEN', confidence: r.confidence,
                title: `${s.schema}.${s.table}.${r.source_column} → ${t.schema}.${t.table}.${r.target_column}`,
                summary_md: `\`${s.table}.${r.source_column}\` references \`${t.table}.${r.target_column}\` (many to one); evidence: model: ${r.rationale}`,
                entities: [r.source_table, r.target_table], element_id: r.id, version: 7, proposed_changes: [] });
        }
    }
    for (const m of o.metrics) {
        status.set(m.id, m.id === 'met_net_revenue' ? 'accepted' : 'proposed');
        if (m.id !== 'met_net_revenue') {
            proposals.push({ id: `fnd_${m.id.slice(4)}`, type: 'METRIC_DEFINITION', kind: 'metric', status: 'OPEN', confidence: m.source === 'bridgeTable' ? 0.9 : 0.72,
                title: `Metric “${m.name}” = ${m.expression.slice(0, 60)}`, summary_md: m.description, entities: m.tables, element_id: m.id, version: 7, proposed_changes: [] });
        }
    }
    proposals.sort((a, b) => b.confidence - a.confidence);
    const junction = new Set(o.bridges.map((b: Json) => b.table_id));
    const termOf = new Map(o.terms.map((t: Json) => [t.table_id, t]));
    const place = (urn: string) => {
        const s = specOf.get(urn) as Spec;
        return { catalog: urn.includes('trino') ? 'lakehouse' : '', schema: s.schema, table_label: `${s.schema}.${s.table}`, domain: DOMAINS[s.domain], domain_name: s.domain, owners: [s.owner] };
    };
    const st = (id: string) => status.get(id) ?? 'compiled';
    const openProposal = (id: string) => proposals.find((p) => p.element_id === id && p.status === 'OPEN')?.id ?? null;
    const relLabel = (r: Json) => `${(specOf.get(r.source_table) as Spec).table}.${r.source_column} → ${(specOf.get(r.target_table) as Spec).table}.${r.target_column}`;

    const explorer = () => ({
        run_id: o.run_id, version: o.version,
        terms: o.terms.map((t: Json) => ({ id: t.id, name: t.name, description: t.description, synonyms: t.synonyms, table: t.table_id, table_name: t.table_name,
            ...place(t.table_id), attributes: t.attribute_ids.length, date_formats: o.attributes.filter((a: Json) => a.term_id === t.id && a.format).length,
            bridge: false, provenance: ['model'], confidence: t.provenance[0].confidence, status: st(t.id), proposal: openProposal(t.id) })),
        relationships: o.relationships.map((r: Json) => ({ id: r.id, source: r.source_table, target: r.target_table, source_column: r.source_column,
            target_column: r.target_column, source_term: termOf.get(r.source_table)?.id ?? null, target_term: termOf.get(r.target_table)?.id ?? null,
            cardinality: r.cardinality, confidence: r.confidence, join_default: r.join_default, provenance: r.provenance.map((p: Json) => p.source),
            queries: r.evidence.queries, status: st(r.id), proposal: openProposal(r.id), bridge: junction.has(r.source_table) })),
        bridges: o.bridges.map((b: Json) => ({ id: b.id, table: b.table_id, table_name: b.table_name, ...place(b.table_id), term: null,
            targets: b.fk_pairs.map((p: Json) => ({ table: p.target_table, column: p.target_column, source_column: p.source_column, term: termOf.get(p.target_table)?.id ?? null })),
            metric: b.metric_id })),
        metrics: o.metrics.map((m: Json) => ({ id: m.id, name: m.name, term: m.term_id, tables: m.tables, source: m.source, status: st(m.id) })),
        tables: SPECS.map((s) => ({ id: urnOf(s), ...place(urnOf(s)), bridge: isBridge(s), term: termOf.get(urnOf(s))?.id ?? null })),
    });

    const evidence = (r: Json) => {
        const src = r.provenance.map((p: Json) => p.source);
        const out: Json = { sources: src };
        if (src.includes('catalog_fk')) out.foreign_key = { detail: r.provenance.find((p: Json) => p.source === 'catalog_fk').detail };
        if (src.includes('observed_in_queries')) out.query_log = { queries: r.evidence.queries, executions: r.evidence.executions, query_ids: r.evidence.query_ids };
        const m = r.provenance.find((p: Json) => p.source === 'model' || p.source === 'inferred');
        if (m) out.model = { confidence: m.confidence, detail: m.detail, inferred_without_model: m.source === 'inferred' };
        return out;
    };

    const termDetail = (id: string) => {
        const t = o.terms.find((x: Json) => x.id === id);
        if (!t) fail(404, 'no such term');
        const table = t.table_id;
        const attrs = o.attributes.filter((a: Json) => a.term_id === id).map((a: Json) => ({ id: a.id, column: a.column, name: a.name, datatype: a.datatype,
            description: a.description, format: a.format, is_unique: a.is_unique, deprecated: false, table, provenance: ['catalog'] }));
        const rels = o.relationships.filter((r: Json) => r.source_table === table || r.target_table === table).map((r: Json) => {
            const out = r.source_table === table;
            const other = out ? r.target_table : r.source_table;
            const ot = termOf.get(other);
            return { id: r.id, direction: out ? 'out' : 'in', source: r.source_table, source_column: r.source_column, target: r.target_table, target_column: r.target_column,
                join: `${(specOf.get(r.source_table) as Spec).table}.${r.source_column} = ${(specOf.get(r.target_table) as Spec).table}.${r.target_column}`,
                other_table: other, other_term: ot ? { id: ot.id, name: ot.name } : null, cardinality: r.cardinality, confidence: r.confidence,
                join_default: r.join_default, rationale: r.rationale, evidence: evidence(r), status: st(r.id) };
        });
        const metrics = o.metrics.filter((m: Json) => m.term_id === id || m.tables.includes(table)).map((m: Json) => ({ id: m.id, name: m.name, description: m.description,
            expression: m.expression, sql: m.sql, source: m.source, usage: m.usage, status: st(m.id) }));
        const related = new Set([id, ...rels.map((r: Json) => r.id), ...metrics.map((m: Json) => m.id)]);
        return {
            id: t.id, name: t.name, description: t.description, synonyms: t.synonyms, proposed_name: t.proposed_name, table, table_name: t.table_name, ...place(table),
            status: st(t.id), glossary_term: st(t.id) === 'accepted' ? `urn:li:glossaryTerm:Ontology.${t.name.replace(/[^A-Za-z0-9]/g, '')}` : null,
            attributes: attrs, date_formats: attrs.filter((a: Json) => a.format).map((a: Json) => ({ column: a.column, name: a.name, format: a.format })),
            relationships: rels, metrics,
            bridges: o.bridges.filter((b: Json) => b.fk_pairs.some((p: Json) => p.target_table === table)).map((b: Json) => ({ id: b.id, table: b.table_id, table_name: b.table_name, targets: b.fk_pairs.map((p: Json) => p.target_table) })),
            provenance: { run: { id: RUN, version: 7, mode: 'incremental', state: 'completed', started_ms: Date.now() - 26 * 3_600_000, finished_ms: Date.now() - 26 * 3_600_000 + 63_000, requested_by: 'urn:li:corpuser:cairn-ontology-scheduler' },
                version: 7, sources: t.provenance, inputs: { tables: [table, ...rels.map((r: Json) => r.other_table)], columns: attrs.map((a: Json) => a.column),
                    queries: rels.flatMap((r: Json) => r.evidence.query_log?.query_ids ?? []) } },
            proposals: proposals.filter((p) => p.status === 'OPEN' && related.has(p.element_id)),
        };
    };

    const decide = (fid: string, action: string, comment: string) => {
        const p = proposals.find((x) => x.id === fid);
        if (!p) fail(404, 'proposal not found');
        if (p.status !== 'OPEN') fail(409, `the proposal is already ${p.status === 'ACCEPTED' ? 'accepted' : 'rejected'}`);
        p.status = action === 'accept' ? 'ACCEPTED' : 'DISMISSED';
        p.review = { action, by: me, comment };
        status.set(p.element_id, action === 'accept' ? 'accepted' : 'rejected');
        return p;
    };

    on('GET', '/v1/ontology/explorer', () => explorer());
    on('GET', '/v1/ontology/terms/:id', (_b, _q, [id]) => termDetail(decodeURIComponent(id)));
    on('GET', '/v1/ontology/asset', (_b, q) => {
        const urn = q.get('urn') ?? '';
        const terms = o.terms.filter((t: Json) => t.table_id === urn);
        return { urn, version: 7, bridge: junction.has(urn),
            terms: terms.map((t: Json) => ({ id: t.id, name: t.name, description: t.description, synonyms: t.synonyms, status: st(t.id) })),
            columns: o.attributes.filter((a: Json) => a.table_id === urn).map((a: Json) => ({ column: a.column, name: a.name, term: a.term_id, term_name: a.term_name, format: a.format, datatype: a.datatype })),
            relationships: o.relationships.filter((r: Json) => r.source_table === urn || r.target_table === urn).map((r: Json) => {
                const out = r.source_table === urn;
                const other = out ? r.target_table : r.source_table;
                const ot = termOf.get(other);
                return { id: r.id, column: out ? r.source_column : r.target_column, other_table: other, other_column: out ? r.target_column : r.source_column,
                    other_term: ot ? { id: ot.id, name: ot.name } : null, cardinality: r.cardinality, confidence: r.confidence };
            }) };
    });
    on('GET', '/v1/ontology/search-terms', (_b, q) => {
        const s = (q.get('q') ?? '').toLowerCase().trim();
        if (!s) return { terms: [] };
        const hits = o.terms.map((t: Json) => {
            const syn = t.synonyms.find((x: string) => x.toLowerCase().includes(s));
            const name = t.name.toLowerCase();
            const score = name === s || syn?.toLowerCase() === s ? 1 : name.startsWith(s) ? 0.8 : name.includes(s) || syn ? 0.6 : 0;
            return { id: t.id, name: t.name, description: t.description, synonyms: t.synonyms, table: t.table_id, table_name: t.table_name,
                matched: syn && !name.includes(s) ? 'synonym' : 'name', synonym: syn ?? null, score };
        }).filter((h: Json) => h.score > 0).sort((a: Json, b: Json) => b.score - a.score || a.name.localeCompare(b.name));
        return { terms: hits.slice(0, Number(q.get('limit') ?? 10)) };
    });
    on('GET', '/v1/ontology/proposals', (_b, q) => {
        const kind = q.get('kind');
        const want = q.get('status') ?? 'OPEN';
        return { proposals: proposals.filter((p) => p.status === want && (!kind || p.kind === kind)) };
    });
    on('POST', '/v1/ontology/proposals/batch', (b) => {
        const results = (b.ids as string[]).map((id) => {
            try {
                return { id, ok: true, status: decide(id, b.action, b.comment ?? '').status };
            } catch (e) {
                return { id, ok: false, status_code: (e as { status?: number }).status ?? 500, error: (e as Error).message };
            }
        });
        const done = results.filter((r) => r.ok).length;
        return { action: b.action, done, refused: results.length - done, results };
    });
    on('POST', '/v1/ontology/proposals/:id/:id', (b, _q, [fid, action]) => {
        if (action !== 'accept' && action !== 'reject') fail(404, 'unknown action');
        const p = decide(decodeURIComponent(fid), action, b?.comment ?? '');
        if (b?.edits?.name && p.kind === 'term') {
            const t = o.terms.find((x: Json) => x.id === p.element_id);
            t.name = b.edits.name;
            if (b.edits.definition) t.description = b.edits.definition;
            if (b.edits.synonyms) t.synonyms = b.edits.synonyms;
        }
        return p;
    });

    // Compiling: the catalog, an estimate, and a run that moves table by table.
    on('GET', '/v1/ontology/catalog', () => {
        const tables = SPECS.map((s) => ({ id: urnOf(s), name: `${s.schema}.${s.table}`, catalog: '', schema: s.schema, columns: s.columns.length }));
        return { tables, schemas: [...new Set(SPECS.map((s) => s.schema))].sort() };
    });
    const scopeSize = (b: Json) => (b?.tables?.length ? b.tables.length : b?.schemas?.length ? SPECS.filter((s) => b.schemas.includes(s.schema)).length : null);
    on('POST', '/v1/ontology/estimate', (b) => {
        const scope = scopeSize(b);
        const inScope = scope ?? SPECS.length;
        const process = b?.mode === 'full' ? inScope : Math.min(inScope, 3);
        return { mode: b?.mode ?? 'incremental', tables: SPECS.length, to_process: process, reused: inScope - process, out_of_scope: SPECS.length - inScope,
            without_columns: 0, scope, published: RUN,
            model: { chat_configured: true, embeddings_configured: true, chat_calls_per_table: 4, chat_calls_max: process * 4, embedding_calls_max: process,
                note: 'an upper bound for the tables compiled; relationships, metrics and bridge tables add calls in proportion to the candidates found' } };
    });
    const runs: Json[] = [
        { id: RUN, version: 7, state: 'completed', mode: 'incremental', started_ms: Date.now() - 26 * 3_600_000, finished_ms: Date.now() - 26 * 3_600_000 + 63_000,
          stats: { tables: SPECS.length, processed: 3, reused: SPECS.length - 3, resumed: 0, skipped: 0, terms: o.terms.length, attributes: o.attributes.length,
                   relationships: o.relationships.length, metrics: o.metrics.length, bridges: o.bridges.length, queries: 1840, seconds: 63.1 }, error: null,
          requested_by: 'urn:li:corpuser:cairn-ontology-scheduler' },
        { id: PREV, version: 6, state: 'completed', mode: 'full', started_ms: Date.now() - 50 * 3_600_000, finished_ms: Date.now() - 50 * 3_600_000 + 410_000,
          stats: { tables: SPECS.length - 1, processed: SPECS.length - 1, reused: 0, resumed: 0, skipped: 0 }, error: null, requested_by: me },
    ];
    const STEP_MS = 700;
    const live = (r: Json) => {
        if (r.state !== 'running') return r;
        const total = r.scope ?? SPECS.length;
        const ticks = Math.floor((Date.now() - r.started_ms - (r.paused_ms ?? 0)) / STEP_MS);
        if (ticks >= total + 4) {
            Object.assign(r, { state: 'completed', finished_ms: Date.now(), stats: { ...r.stats, tables: SPECS.length, processed: total, reused: SPECS.length - total, terms: o.terms.length } });
            delete r.progress;
            return r;
        }
        r.progress = { run_id: r.id, phase: ticks < total ? 'tables' : ['relationships', 'metrics', 'bridges', 'publishing'][ticks - total],
            tables_total: SPECS.length, tables_done: Math.min(ticks, total), scope: r.scope };
        return r;
    };
    on('GET', '/v1/ontology/runs', () => ({ runs: runs.map(live), published: runs.find((r) => r.state === 'completed')?.id ?? null, active: runs.find((r) => r.state === 'running')?.id ?? null }));
    on('POST', '/v1/ontology/runs', (b) => {
        if (runs.some((r) => live(r).state === 'running')) fail(409, `run ${runs[0].id} is in progress`);
        const run = { id: `run_${Math.random().toString(16).slice(2, 18)}`, version: runs[0].version + 1, state: 'running', mode: b?.mode ?? 'incremental',
            started_ms: Date.now(), finished_ms: null, stats: {}, error: null, requested_by: me, scope: scopeSize(b) };
        runs.unshift(run);
        return { run: live(run), sync: { catalog: { tables: SPECS.length }, queries: { received: 12 } } };
    });
    const runById = (id: string) => runs.find((r) => r.id === decodeURIComponent(id)) ?? fail(404, 'no such run');
    on('GET', '/v1/ontology/runs/:id/diff', (_b, _q, [id]) => {
        const r = runById(id);
        if (live(r).state !== 'completed') fail(409, `run ${r.id} is ${r.state}: only a completed run has an ontology`);
        const newRel = o.relationships.find((x: Json) => `${(specOf.get(x.source_table) as Spec).table}.${x.source_column}` === NEW_REL);
        const late = o.terms.find((t: Json) => t.id === 'term_stock_movements');
        return {
            run: r.id, against: PREV, version: r.version,
            terms: { added: [{ id: late.id, label: late.name }], removed: [{ id: 'term_legacy_orders', label: 'Legacy Order' }],
                changed: [{ id: 'term_customers', label: 'Customer', changes: { synonyms: { before: ['client'], after: ['buyer', 'client'] } } }],
                counts: { added: 1, removed: 1, changed: 1, unchanged: o.terms.length - 2 } },
            relationships: { added: [{ id: newRel.id, label: relLabel(newRel) }], removed: [],
                changed: [{ id: 'rel_orders_customer_id', label: 'orders.customer_id → customers.id', changes: { provenance: { before: ['catalog_fk'], after: ['catalog_fk', 'observed_in_queries'] } } }],
                counts: { added: 1, removed: 0, changed: 1, unchanged: o.relationships.length - 2 } },
            metrics: { added: [{ id: 'met_outstanding', label: 'Outstanding Balance' }], removed: [], changed: [], counts: { added: 1, removed: 0, changed: 0, unchanged: o.metrics.length - 1 } },
        };
    });
    on('GET', '/v1/ontology/runs/:id', (_b, _q, [id]) => live(runById(id)));
    on('POST', '/v1/ontology/runs/:id/cancel', (_b, _q, [id]) => {
        const r = live(runById(id));
        if (r.state !== 'running') fail(409, 'that run is not in progress');
        Object.assign(r, { state: 'cancelled', finished_ms: Date.now(), cancelled_at: Date.now(), stats: { skipped: (r.scope ?? SPECS.length) - (r.progress?.tables_done ?? 0) } });
        return { cancelling: r.id };
    });
    on('POST', '/v1/ontology/runs/:id/resume', (_b, _q, [id]) => {
        const r = runById(id);
        if (!['cancelled', 'interrupted', 'failed'].includes(r.state)) fail(409, `a ${r.state} run cannot be resumed`);
        r.paused_ms = (r.paused_ms ?? 0) + (Date.now() - (r.cancelled_at ?? Date.now()));
        Object.assign(r, { state: 'running', finished_ms: null });
        return live(r);
    });
    on('POST', '/v1/ontology/runs/:id/import', (_b, _q, [id]) => ({ run_id: runById(id).id, version: 8, new: 3, updated: proposals.length, unchanged_dismissed: 0 }));

    // Exports.
    on('GET', '/v1/ontology/graph', (_b, q) => {
        const fmt = q.get('format') ?? 'json';
        const e = explorer();
        if (fmt === 'csv') {
            const rows = [['kind', 'id', 'name', 'table', 'column', 'related_table', 'related_column', 'cardinality', 'confidence'],
                ...e.terms.map((t) => ['term', t.id, t.name, t.table, '', '', '', '', String(t.confidence)]),
                ...e.relationships.map((r) => ['relationship', r.id, `${r.source_column} = ${r.target_column}`, r.source, r.source_column, r.target, r.target_column, r.cardinality, String(r.confidence)])];
            return rows.map((r) => r.map((c) => (/[",\n]/.test(c) ? `"${c.replace(/"/g, '""')}"` : c)).join(',')).join('\n');
        }
        if (fmt === 'turtle') {
            return ['@prefix owl: <http://www.w3.org/2002/07/owl#> .', '@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .',
                '@prefix skos: <http://www.w3.org/2004/02/skos/core#> .', '@prefix cairn: <urn:cairn:ontology:> .', '',
                ...e.terms.map((t) => `cairn:term_${t.id.slice(5)} a owl:Class ; rdfs:label "${t.name}"${t.synonyms.map((s: string) => ` ; skos:altLabel "${s}"`).join('')} .`)].join('\n');
        }
        if (fmt === 'jsonld') {
            return { '@context': { owl: 'http://www.w3.org/2002/07/owl#', rdfs: 'http://www.w3.org/2000/01/rdf-schema#', skos: 'http://www.w3.org/2004/02/skos/core#' },
                '@graph': e.terms.map((t) => ({ '@id': `urn:cairn:ontology:term:${t.id}`, '@type': 'owl:Class', 'rdfs:label': t.name, 'skos:altLabel': t.synonyms })) };
        }
        return { run_id: o.run_id, version: o.version,
            nodes: [...e.terms.map((t) => ({ id: t.id, kind: 'term', label: t.name, description: t.description, synonyms: t.synonyms })),
                ...e.tables.map((t) => ({ id: t.id, kind: 'table', label: t.table_label }))],
            edges: [...e.terms.map((t) => ({ source: t.id, target: t.table, kind: 'represents' })),
                ...e.relationships.map((r) => ({ source: r.source, target: r.target, kind: 'references', id: r.id, on: `${r.source_column} = ${r.target_column}`,
                    confidence: r.confidence, provenance: r.provenance, source_term: r.source_term, target_term: r.target_term }))] };
    });
}

/** What the demo's Ask-your-data answer about orders was grounded in. */
export const DEMO_GROUNDED = {
    terms: [{ id: 'term_orders', name: 'Order', table: ds('snowflake', 'analytics.core.orders') }],
    relationships: [{ id: 'rel_orders_customer_id', label: 'orders.customer_id → customers.id' }],
};
