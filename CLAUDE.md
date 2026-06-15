# CLAUDE.md

Project context for Claude Code. Read this before doing anything.

## What this project is

SMB Cash + AR analytics dashboard, v1. Pilot tenant: Industrial Pipe & Supply,
who run QuickBooks Desktop and export four CSV reports. The product promise is
trust: the AR total shown on the dashboard must tie to the customer's QuickBooks
A/R Aging Summary total, to the cent, before any dashboard is shown.

The trust-test number for the current snapshot (2026-05-31) is **$609,772.89**.

## Current state (already built and verified, do not rebuild)

The parse-and-reconcile core is done and passing. These files exist and work:

- `qbd_core.py` - shared primitives: per-file encoding detection, money/date/name
  parsing (all money is `Decimal`, never float), hashing, shared dataclasses.
- `qbd_reports.py` - parsers for Aging Summary, Aging Detail, Open Invoices.
- `qbd_cbd.py` - parser for Customer Balance Detail.
- `qbd_reconcile.py` - the six-check reconciliation gate.
- `run_batch.py` - runnable entrypoint; parses `./ips_files` and prints the gate.
- `test_batch.py` - 16 tests, all passing, including failure-path tests.

Run `python3 run_batch.py ips_files` to see all six checks pass and GATE PASSED.
Run `python3 -m pytest test_batch.py -v` to confirm 16 passed.

Stage 3, the canonical Postgres load, is also done and verified:

- `schema.sql` - the five canonical tables: batches, customers, invoices (with the
  generated delta column), ar_transactions, ar_aging_snapshots.
- `load_canonical.py` - transactional, idempotent loader (per-file sha256 no-op),
  plus a read-back layer that reconstructs the gate's inputs from the tables and
  runs the existing reconciliation gate against Postgres, unmodified.
- `test_canonical.py` - 15 tests, all passing, including failure-path tests
  (tampered amounts fail the gate, bad rows roll back the whole load).
- `docker-compose.yml` - local Postgres 16 for development, host port 5433. The
  connection string is read from the DATABASE_URL environment variable only.

Run `docker compose up -d`, set DATABASE_URL, then `python load_canonical.py
ips_files` to see CANONICAL GATE PASSED at 609,772.89. Run
`python -m pytest test_canonical.py -v` to confirm 15 passed.

Stage 5, the serving marts, is also done and verified:

- `marts.sql` - the gold-layer objects over canonical only: five materialized
  views (mart_ar_summary, mart_ar_aging_by_customer, mart_top_overdue,
  mart_credits_unapplied, mart_ar_trend), two helper views for deterministic
  snapshot selection, and the mart_refresh_stamps table. tenant_id on every
  mart, all money NUMERIC(19,4).
- `refresh_marts.py` - runs the reconciliation gate first, then refreshes the
  marts and writes the stamp in one transaction. A failed gate blocks the
  refresh entirely: nothing is written, the last good snapshot stays live, and
  the stamp table has a DB-level CHECK (gate_passed) so only passing runs can
  ever be stamped. Exit codes: 0 success, 1 environment or snapshot problem,
  2 gate-blocked.
- `test_marts.py` - 8 tests, all passing, including the failure path (a
  tampered canonical amount blocks the refresh and leaves marts and stamps
  byte-identical).

Run `python refresh_marts.py --tenant ips` to see the stamp print with
control_total 609,772.89 and delta 0.00. Run `python -m pytest test_marts.py -v`
to confirm 8 passed.

The dashboard reads only from the marts, never from canonical tables or CSVs.

The real CSVs live in `./ips_files/`. They are the source of truth. Never edit them.

## Verified facts about the real data (do not re-derive, do not assume otherwise)

- Four reports, all dated 2026-05-31. Three are ASCII, Customer Balance Detail
  is cp1252. Encoding is detected per file.
- Three reports tie to 609,772.89: Aging Summary, Aging Detail, and the sum of
  Customer Balance Detail customer endings.
- Open Invoices totals 524,144.48. The 85,628.41 gap is fully explained by 32
  invoices paid between the as-of date and the export date. This is a known QBD
  quirk, not an error, and the gate already handles it.
- The `> 90` bucket is composed of 31 legacy General Journal entries that net to
  -13,341.66. `General Journal` is a real transaction type and is handled.
- Transaction types in AR: Invoice (increases AR), Payment, Discount, Credit Memo
  (all decrease), General Journal (sign of the amount carries direction).
- Customer names are the only join key and they are dirty (double spaces, trailing
  dashes, a source typo "Supression"). Names are normalized for matching; the raw
  name is preserved. Typos are surfaced for review, never silently rewritten.
- The grand TOTAL row sits flush-left in column 0 in Customer Balance Detail and
  Open Invoices, but indented in the Aging reports. Both cases are handled.
- QuickBooks ages invoices from the transaction date, not the due date. Proven
  against all 149 real Aging Detail rows and locked by a test; the rule lives in
  `derive_bucket` in `load_canonical.py`.

## Architecture rules (non-negotiable, from the spec and the decisions log)

- All money columns are `NUMERIC(19,4)` in Postgres. Never the `money` type,
  never float.
- The invoice reconciliation delta is a generated column:
  `GENERATED ALWAYS AS (computed_balance - reported_balance) STORED`.
- One `ar_transactions` ledger. Amounts stored as positive magnitudes with a
  `CHECK (amount > 0)`; direction comes from the transaction type. General Journal
  is the exception that carries its own sign, so model it explicitly.
- `tenant_id` on every table. v1 has one tenant but the schema is multi-tenant.
- Every row carries the `batch_id` that produced it, for lineage and rollback.
- Medallion layering: raw (immutable) -> validate-before-write -> canonical ->
  serving marts -> dashboard. Raw data never reaches the dashboard directly.
- Validation failures quarantine the whole file with a machine-readable reason.
  Never silently coerce bad data.
- Reconciliation runs before any serving refresh. A failed gate blocks the
  refresh and keeps the last good snapshot live.

## Scope guardrails (v1 only)

- v1 is Cash + AR only. Inventory, custom UI, OCR, and agents are v2+. Do not add
  them, do not scaffold for them, do not suggest them mid-task.
- Managed Postgres with tested backups. No self-hosted database.
- Manual trigger is fine for v1. Do not build folder-watching or scheduling.

## Code style

- Plain, readable Python. No clever one-liners where a clear loop is better.
- No em dashes, no double dashes, no emojis anywhere in code or comments.
- No end-of-file summary comments unless asked.
- Tests for every new module, including failure-path tests that prove a check
  fails when it should. A gate that only ever passes is untested.
- When you finish a task, run the tests and the batch report, and paste the
  output. Done means the gate ties, not that the code is written.
