-- Demo "private" source database living only on the vpc-finance network. The remote agent
-- ingests it; nothing in the control plane can reach it directly.
CREATE SCHEMA IF NOT EXISTS finance;

CREATE TABLE IF NOT EXISTS finance.customers (
  customer_id   BIGINT PRIMARY KEY,
  email         TEXT NOT NULL,
  phone_number  TEXT,
  country       TEXT NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE finance.customers IS 'Customer master data (contains PII)';

CREATE TABLE IF NOT EXISTS finance.invoices (
  invoice_id    BIGINT PRIMARY KEY,
  customer_id   BIGINT NOT NULL REFERENCES finance.customers(customer_id),
  amount_cents  BIGINT NOT NULL,
  currency      CHAR(3) NOT NULL,
  issued_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE VIEW finance.revenue_by_country AS
SELECT c.country, date_trunc('day', i.issued_at) AS day, sum(i.amount_cents) AS revenue_cents
FROM finance.invoices i JOIN finance.customers c USING (customer_id)
GROUP BY 1, 2;

INSERT INTO finance.customers (customer_id, email, phone_number, country)
SELECT g, 'user' || g || '@example.com', '+1-555-' || lpad(g::text, 4, '0'),
       (ARRAY['US','DE','IN','BR','JP'])[1 + g % 5]
FROM generate_series(1, 500) g
ON CONFLICT DO NOTHING;

INSERT INTO finance.invoices (invoice_id, customer_id, amount_cents, currency, issued_at)
SELECT g, 1 + g % 500, (random() * 100000)::bigint, 'USD', now() - (g || ' minutes')::interval
FROM generate_series(1, 5000) g
ON CONFLICT DO NOTHING;

-- Read-only role used by the agent (ingestion) and by DQ monitors.
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'datahub_reader') THEN
    CREATE ROLE datahub_reader LOGIN PASSWORD 'reader-demo-only';
  END IF;
END$$;
GRANT USAGE ON SCHEMA finance TO datahub_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA finance TO datahub_reader;
