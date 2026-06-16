/* Serving marts (Stage 5, gold layer). Schema-unqualified so the file can be
   applied into any search_path target (dev schema, per-test schema). All DDL
   is idempotent. Materialized views are created WITH NO DATA and are only
   ever populated by refresh_marts.py, inside one transaction, after the
   reconciliation gate passes. */

/* One row per successful gated refresh, written in the same transaction as
   the REFRESH statements. gate_passed is CHECKed true because a failed gate
   must never produce a stamp. */
CREATE TABLE IF NOT EXISTS mart_refresh_stamps (
    stamp_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    as_of_date DATE NOT NULL,
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    control_total NUMERIC(19,4) NOT NULL,
    computed_total NUMERIC(19,4) NOT NULL,
    delta NUMERIC(19,4) GENERATED ALWAYS AS (control_total - computed_total) STORED,
    gate_passed BOOLEAN NOT NULL CHECK (gate_passed),
    checks_passed INT NOT NULL,
    checks_total INT NOT NULL,
    summary_batch_id UUID NOT NULL REFERENCES batches (batch_id),
    detail_batch_id UUID NOT NULL REFERENCES batches (batch_id),
    open_invoices_batch_id UUID NOT NULL REFERENCES batches (batch_id),
    cbd_batch_id UUID NOT NULL REFERENCES batches (batch_id)
);

/* Helper view: one batch per report_type for every complete snapshot. A
   (tenant_id, as_of_date) pair qualifies only when all four report types are
   loaded; if the same report was loaded more than once the newest batch wins. */
CREATE OR REPLACE VIEW mart_snapshot_batches AS
WITH loaded AS (
    SELECT tenant_id, as_of_date, report_type, batch_id, received_at
    FROM batches
    WHERE status = 'loaded'
      AND report_type IN ('aging_summary', 'aging_detail',
                          'open_invoices', 'customer_balance_detail')
),
complete_snapshots AS (
    SELECT tenant_id, as_of_date
    FROM loaded
    GROUP BY tenant_id, as_of_date
    HAVING count(DISTINCT report_type) = 4
)
SELECT DISTINCT ON (l.tenant_id, l.as_of_date, l.report_type)
    l.tenant_id,
    l.as_of_date,
    l.report_type,
    l.batch_id
FROM loaded l
JOIN complete_snapshots c
  ON c.tenant_id = l.tenant_id AND c.as_of_date = l.as_of_date
ORDER BY l.tenant_id, l.as_of_date, l.report_type, l.received_at DESC, l.batch_id DESC;

/* Helper view: the latest complete snapshot per tenant. */
CREATE OR REPLACE VIEW mart_current_batches AS
SELECT s.tenant_id, s.as_of_date, s.report_type, s.batch_id
FROM mart_snapshot_batches s
JOIN (
    SELECT tenant_id, max(as_of_date) AS as_of_date
    FROM mart_snapshot_batches
    GROUP BY tenant_id
) latest
  ON latest.tenant_id = s.tenant_id AND latest.as_of_date = s.as_of_date;

/* Per-customer aging pivot of the current Aging Summary batch. */
CREATE MATERIALIZED VIEW IF NOT EXISTS mart_ar_aging_by_customer AS
WITH pivot AS (
    SELECT
        s.tenant_id,
        s.as_of_date,
        s.customer_id,
        c.raw_name,
        c.normalized_name,
        CAST(COALESCE(SUM(s.amount) FILTER (WHERE s.bucket = 'Current'), 0) AS NUMERIC(19,4)) AS bucket_current,
        CAST(COALESCE(SUM(s.amount) FILTER (WHERE s.bucket = '1 - 30'), 0) AS NUMERIC(19,4)) AS bucket_1_30,
        CAST(COALESCE(SUM(s.amount) FILTER (WHERE s.bucket = '31 - 60'), 0) AS NUMERIC(19,4)) AS bucket_31_60,
        CAST(COALESCE(SUM(s.amount) FILTER (WHERE s.bucket = '61 - 90'), 0) AS NUMERIC(19,4)) AS bucket_61_90,
        CAST(COALESCE(SUM(s.amount) FILTER (WHERE s.bucket = '> 90'), 0) AS NUMERIC(19,4)) AS bucket_over_90,
        CAST(COALESCE(SUM(s.amount) FILTER (WHERE s.bucket = 'Total'), 0) AS NUMERIC(19,4)) AS total
    FROM ar_aging_snapshots s
    JOIN mart_current_batches b
      ON b.tenant_id = s.tenant_id AND b.batch_id = s.batch_id
     AND b.report_type = 'aging_summary'
    JOIN customers c ON c.customer_id = s.customer_id
    GROUP BY s.tenant_id, s.as_of_date, s.customer_id, c.raw_name, c.normalized_name
)
SELECT
    tenant_id,
    as_of_date,
    customer_id,
    raw_name,
    normalized_name,
    bucket_current,
    bucket_1_30,
    bucket_31_60,
    bucket_61_90,
    bucket_over_90,
    total,
    CAST(bucket_31_60 + bucket_61_90 + bucket_over_90 AS NUMERIC(19,4)) AS overdue_31_plus,
    ROW_NUMBER() OVER (
        PARTITION BY tenant_id
        ORDER BY (bucket_31_60 + bucket_61_90 + bucket_over_90) DESC, normalized_name ASC
    ) AS overdue_rank
FROM pivot
ORDER BY tenant_id, overdue_rank
WITH NO DATA;

/* Top ten genuinely overdue customers; refresh after mart_ar_aging_by_customer. */
CREATE MATERIALIZED VIEW IF NOT EXISTS mart_top_overdue AS
SELECT
    tenant_id,
    as_of_date,
    customer_id,
    raw_name,
    normalized_name,
    bucket_31_60,
    bucket_61_90,
    bucket_over_90,
    total,
    overdue_31_plus,
    overdue_rank
FROM (
    SELECT
        tenant_id,
        as_of_date,
        customer_id,
        raw_name,
        normalized_name,
        bucket_31_60,
        bucket_61_90,
        bucket_over_90,
        total,
        overdue_31_plus,
        ROW_NUMBER() OVER (
            PARTITION BY tenant_id
            ORDER BY overdue_31_plus DESC, normalized_name ASC
        ) AS overdue_rank
    FROM mart_ar_aging_by_customer
    WHERE overdue_31_plus > 0
) ranked
WHERE overdue_rank <= 10
ORDER BY tenant_id, overdue_rank
WITH NO DATA;

/* One headline row per tenant. INNER join to the latest stamp on purpose:
   the mart stays empty until the first successful gated refresh. */
CREATE MATERIALIZED VIEW IF NOT EXISTS mart_ar_summary AS
WITH totals AS (
    SELECT
        s.tenant_id,
        s.as_of_date,
        CAST(COALESCE(SUM(s.amount) FILTER (WHERE s.bucket = 'Total'), 0) AS NUMERIC(19,4)) AS total_ar,
        CAST(COALESCE(SUM(s.amount) FILTER (WHERE s.bucket = 'Current'), 0) AS NUMERIC(19,4)) AS bucket_current,
        CAST(COALESCE(SUM(s.amount) FILTER (WHERE s.bucket = '1 - 30'), 0) AS NUMERIC(19,4)) AS bucket_1_30,
        CAST(COALESCE(SUM(s.amount) FILTER (WHERE s.bucket = '31 - 60'), 0) AS NUMERIC(19,4)) AS bucket_31_60,
        CAST(COALESCE(SUM(s.amount) FILTER (WHERE s.bucket = '61 - 90'), 0) AS NUMERIC(19,4)) AS bucket_61_90,
        CAST(COALESCE(SUM(s.amount) FILTER (WHERE s.bucket = '> 90'), 0) AS NUMERIC(19,4)) AS bucket_over_90
    FROM ar_aging_snapshots s
    JOIN mart_current_batches b
      ON b.tenant_id = s.tenant_id AND b.batch_id = s.batch_id
     AND b.report_type = 'aging_summary'
    GROUP BY s.tenant_id, s.as_of_date
),
latest_stamp AS (
    SELECT DISTINCT ON (tenant_id)
        tenant_id, refreshed_at, delta, gate_passed
    FROM mart_refresh_stamps
    ORDER BY tenant_id, stamp_id DESC
)
SELECT
    t.tenant_id,
    t.as_of_date,
    t.total_ar,
    t.bucket_current,
    t.bucket_1_30,
    t.bucket_31_60,
    t.bucket_61_90,
    t.bucket_over_90,
    st.delta AS recon_delta,
    st.gate_passed,
    st.refreshed_at
FROM totals t
JOIN latest_stamp st ON st.tenant_id = t.tenant_id
WITH NO DATA;

/* Genuine unapplied credits of the current snapshot: only customers whose
   Aging Summary Total is negative (overpayments / unapplied credits). The
   prior definition also UNION'd every AR-decreasing item from Aging Detail,
   which double-counted historical journal entries (the 2011 Old System
   entries netting to -13,341.66) that are already reconciled into the AR
   total. Net-negative balance is the only true signal of a current credit. */
CREATE MATERIALIZED VIEW IF NOT EXISTS mart_credits_unapplied AS
SELECT
    s.tenant_id,
    s.as_of_date,
    'negative_balance' AS source,
    c.raw_name,
    c.normalized_name,
    CAST(NULL AS TEXT) AS txn_type,
    CAST(NULL AS TEXT) AS invoice_number,
    CAST(NULL AS DATE) AS txn_date,
    CAST(s.amount AS NUMERIC(19,4)) AS signed_amount
FROM ar_aging_snapshots s
JOIN mart_current_batches b
  ON b.tenant_id = s.tenant_id AND b.batch_id = s.batch_id
 AND b.report_type = 'aging_summary'
JOIN customers c ON c.customer_id = s.customer_id
WHERE s.bucket = 'Total' AND s.amount < 0
WITH NO DATA;

/* AR trend across every complete snapshot, with prior-snapshot deltas. */
CREATE MATERIALIZED VIEW IF NOT EXISTS mart_ar_trend AS
WITH per_snapshot AS (
    SELECT
        s.tenant_id,
        b.as_of_date,
        CAST(COALESCE(SUM(s.amount) FILTER (WHERE s.bucket = 'Total'), 0) AS NUMERIC(19,4)) AS total_ar,
        CAST(COALESCE(SUM(s.amount) FILTER (WHERE s.bucket = 'Current'), 0) AS NUMERIC(19,4)) AS bucket_current,
        CAST(COALESCE(SUM(s.amount) FILTER (WHERE s.bucket = '1 - 30'), 0) AS NUMERIC(19,4)) AS bucket_1_30,
        CAST(COALESCE(SUM(s.amount) FILTER (WHERE s.bucket = '31 - 60'), 0) AS NUMERIC(19,4)) AS bucket_31_60,
        CAST(COALESCE(SUM(s.amount) FILTER (WHERE s.bucket = '61 - 90'), 0) AS NUMERIC(19,4)) AS bucket_61_90,
        CAST(COALESCE(SUM(s.amount) FILTER (WHERE s.bucket = '> 90'), 0) AS NUMERIC(19,4)) AS bucket_over_90
    FROM ar_aging_snapshots s
    JOIN mart_snapshot_batches b
      ON b.tenant_id = s.tenant_id AND b.batch_id = s.batch_id
     AND b.report_type = 'aging_summary'
    GROUP BY s.tenant_id, b.as_of_date
)
SELECT
    tenant_id,
    as_of_date,
    total_ar,
    bucket_current,
    bucket_1_30,
    bucket_31_60,
    bucket_61_90,
    bucket_over_90,
    CAST(LAG(total_ar) OVER w AS NUMERIC(19,4)) AS prev_total_ar,
    CAST(total_ar - LAG(total_ar) OVER w AS NUMERIC(19,4)) AS total_ar_change,
    CAST(bucket_current - LAG(bucket_current) OVER w AS NUMERIC(19,4)) AS bucket_current_change,
    CAST(bucket_1_30 - LAG(bucket_1_30) OVER w AS NUMERIC(19,4)) AS bucket_1_30_change,
    CAST(bucket_31_60 - LAG(bucket_31_60) OVER w AS NUMERIC(19,4)) AS bucket_31_60_change,
    CAST(bucket_61_90 - LAG(bucket_61_90) OVER w AS NUMERIC(19,4)) AS bucket_61_90_change,
    CAST(bucket_over_90 - LAG(bucket_over_90) OVER w AS NUMERIC(19,4)) AS bucket_over_90_change
FROM per_snapshot
WINDOW w AS (PARTITION BY tenant_id ORDER BY as_of_date)
ORDER BY tenant_id, as_of_date
WITH NO DATA;
