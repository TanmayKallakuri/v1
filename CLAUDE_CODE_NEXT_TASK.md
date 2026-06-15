# Claude Code kickoff prompt: canonical Postgres load (Stage 3)

Paste the block below into Claude Code as your first message. It is scoped to one
task: take the already-verified parsed records and write them into a canonical
PostgreSQL schema, plus the batch registry. It deliberately does NOT touch the
dashboard, the marts, or anything in v2.

---

PROMPT TO PASTE:

Read CLAUDE.md first. The parse-and-reconcile core is already built and passing;
do not modify it except to import from it.

Build Stage 3, the canonical Postgres load. Specifically:

1. A `schema.sql` file with the canonical tables:
   - `batches` (load registry): batch_id, tenant_id, filename, report_type,
     as_of_date, sha256, received_at, detected_encoding, row_count,
     quarantine_count, status.
   - `customers`: tenant_id, customer_id, normalized_name, raw_name, terms.
   - `invoices`: tenant_id, batch_id, invoice_number, customer_id, txn_date,
     due_date, terms, reported_balance, computed_balance, and a generated delta
     column.
   - `ar_transactions`: tenant_id, batch_id, customer_id, invoice_number
     (nullable), txn_type, amount (positive magnitude, CHECK amount > 0),
     direction, txn_date.
   - `ar_aging_snapshots`: tenant_id, batch_id, customer_id, bucket, amount,
     as_of_date, captured_at.
   - All money is NUMERIC(19,4). delta is GENERATED ALWAYS AS STORED. tenant_id
     and batch_id on every fact table.

2. A `load_canonical.py` module that takes the parsed report objects from the
   existing parsers and writes them into the schema, inside a transaction, with
   idempotency: if a file's sha256 already exists in `batches` for this tenant,
   the load is a no-op.

3. Use a local Postgres via Docker for development. Provide a `docker-compose.yml`
   for a Postgres 16 container and read the connection string from an environment
   variable, never hardcoded.

4. After loading, run the existing reconciliation gate against the canonical
   tables (not the CSVs) and confirm it still ties to 609,772.89. Add a test that
   loads the real batch into a test database and asserts the canonical AR total
   equals 609,772.89.

Constraints:
- DO NOT add new features, only implement what is specified above.
- DO NOT build serving marts, the dashboard, scheduling, or anything v2.
- DO NOT modify the existing parser or reconciliation files except to import them.
- Follow every rule in CLAUDE.md (NUMERIC, generated delta, single ledger,
  tenant_id, no em dashes, failure-path tests).
- When done, run the tests and paste the output showing the canonical load ties
  to 609,772.89.

---

## Tips for the session

- If Claude Code proposes adding anything beyond the five tables and the loader
  (an API, a mart, a scheduler), stop it and point back to the scope guardrails.
- The single most important acceptance criterion is the same as always: after the
  load, the reconciliation gate run against the Postgres tables still says
  609,772.89, delta 0.00. If that holds, Stage 3 is done.
- Keep the `ar_transactions` direction logic honest. Invoice increases AR;
  Payment, Discount, Credit Memo decrease it; General Journal carries its own
  sign. This is already encoded in `qbd_reports.TXN_TYPE_DIRECTION`; reuse it,
  do not redefine it.
