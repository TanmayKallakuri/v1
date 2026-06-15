/* Canonical AR schema (Stage 3). Schema-unqualified so it can be applied
   into any search_path target (dev schema, per-test schema). */

CREATE TABLE IF NOT EXISTS batches (
    batch_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    report_type TEXT NOT NULL,
    as_of_date DATE NOT NULL,
    sha256 TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    detected_encoding TEXT NOT NULL,
    /* row_count = fact rows written to the canonical tables for this batch:
       snapshot rows for summary and CBD, ledger rows for detail and open invoices */
    row_count INT NOT NULL,
    quarantine_count INT NOT NULL,
    status TEXT NOT NULL,
    UNIQUE (tenant_id, sha256)
);

CREATE TABLE IF NOT EXISTS customers (
    customer_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    raw_name TEXT NOT NULL,
    terms TEXT,
    UNIQUE (tenant_id, normalized_name)
);

CREATE TABLE IF NOT EXISTS invoices (
    invoice_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    batch_id UUID NOT NULL REFERENCES batches (batch_id),
    invoice_number TEXT NOT NULL,
    customer_id BIGINT NOT NULL REFERENCES customers (customer_id),
    txn_date DATE,
    due_date DATE,
    terms TEXT,
    reported_balance NUMERIC(19,4) NOT NULL,
    computed_balance NUMERIC(19,4) NOT NULL,
    delta NUMERIC(19,4) GENERATED ALWAYS AS (computed_balance - reported_balance) STORED
);

CREATE INDEX IF NOT EXISTS invoices_tenant_batch_number_idx
    ON invoices (tenant_id, batch_id, invoice_number);

CREATE TABLE IF NOT EXISTS ar_transactions (
    txn_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    batch_id UUID NOT NULL REFERENCES batches (batch_id),
    customer_id BIGINT NOT NULL REFERENCES customers (customer_id),
    invoice_number TEXT,
    txn_type TEXT NOT NULL,
    amount NUMERIC(19,4) NOT NULL CHECK (amount > 0),
    direction TEXT NOT NULL CHECK (direction IN ('increase', 'decrease')),
    txn_date DATE,
    /* qbd_bucket is the aging bucket as reported by QuickBooks in the Aging
       Detail export. It is the authoritative source of truth for aging: we
       store what QBD said rather than recomputing it, because the product
       promise is that our numbers tie to QuickBooks. Nullable because only the
       Aging Detail report carries a bucket; Open Invoices items have none. */
    qbd_bucket TEXT CHECK (qbd_bucket IN ('Current', '1 - 30', '31 - 60', '61 - 90', '> 90'))
);

CREATE INDEX IF NOT EXISTS ar_transactions_tenant_batch_idx
    ON ar_transactions (tenant_id, batch_id);

CREATE TABLE IF NOT EXISTS ar_aging_snapshots (
    tenant_id TEXT NOT NULL,
    batch_id UUID NOT NULL REFERENCES batches (batch_id),
    customer_id BIGINT NOT NULL REFERENCES customers (customer_id),
    bucket TEXT NOT NULL CHECK (bucket IN ('Current', '1 - 30', '31 - 60', '61 - 90', '> 90', 'Total')),
    amount NUMERIC(19,4) NOT NULL,
    as_of_date DATE NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, batch_id, customer_id, bucket)
);
