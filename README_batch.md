# QBD v1 ingestion and reconciliation

Parses all four QuickBooks Desktop AR reports from the Industrial Pipe & Supply
batch and runs the six-check reconciliation gate from the workflow doc. The gate
passes only when the AR total ties to the customer's QuickBooks A/R Aging Summary
total, to the cent, across every cross-check.

## Verified result (real batch, 2026-05-31)

```
Aging Summary            enc=ascii   grand_total=609772.89   quarantine=0
Aging Detail             enc=ascii   grand_total=609772.89   quarantine=0
Open Invoices            enc=ascii   grand_total=524144.48   quarantine=0
Customer Balance Detail  enc=cp1252  grand_total=609772.89   quarantine=0

[PASS] Keystone (computed AR = Aging Summary TOTAL)            delta 0.00
[PASS] Cross-report A (CBD endings = Aging Summary TOTAL)      delta 0.00
[PASS] Cross-report B (Aging Detail TOTAL = Aging Summary)     delta 0.00
[PASS] Bucket integrity (recomputed buckets match summary)
[PASS] Open Invoices delta explained by post-period activity  delta 0.00
[PASS] Per-customer (computed = Aging Summary row)
GATE PASSED
```

The Open Invoices check independently rediscovers the quirk: the report totals
524,144.48 versus the Aging total of 609,772.89, and the 85,628.41 gap is fully
explained by 32 invoices paid between the 05/31 as-of date and the export date.
Residual 0.00.

## Files

| File | Purpose |
|------|---------|
| `qbd_core.py` | Shared primitives: encoding detection, money/date/name parsing, hashing, dataclasses. All money is Decimal. |
| `qbd_reports.py` | One parser per report type: Aging Summary, Aging Detail, Open Invoices. Column positions verified against the real files. |
| `qbd_cbd.py` | Customer Balance Detail parser (per-customer ledger with running balances). |
| `qbd_reconcile.py` | The six-check reconciliation gate, including the Open Invoices post-period quirk logic. |
| `run_batch.py` | Runnable entrypoint: parses a folder of the four CSVs and prints the gate report. |
| `test_batch.py` | Full pytest suite: parsers, the gate on real data, and failure paths. |

## How to test the code

### 1. Prerequisites

Python 3.10+ and pytest:

```bash
pip install pytest
```

Put the four real CSVs in a subfolder named `ips_files` next to these scripts:

```
ips_files/
  IndustrialPipe_AR-Aging-Summary_2026-05-31.csv
  IndustrialPipe_AR-Aging-Detail_2026-05-31.csv
  IndustrialPipe_OpenInvoices _2026-05-31.csv
  IndustrialPipe_CustomerBalanceDetail_2026_05.csv
```

### 2. Run the reconciliation report (the demo)

```bash
python3 run_batch.py ips_files
```

You should see all six checks print `[PASS]` and `GATE PASSED`. The process exit
code is 0 on pass and 1 on fail, so it can gate a downstream refresh directly:

```bash
python3 run_batch.py ips_files && echo "refresh dashboard" || echo "refresh blocked"
```

### 3. Run the test suite

```bash
python3 -m pytest test_batch.py -v
```

Expected: 16 passed. The suite has three layers:

- **Core primitives** (no data needed): money parsing including negatives and
  parenthesized negatives, name normalization, date parsing, garbage rejection.
- **Real-file tests** (need `ips_files`): each parser produces the verified
  totals; General Journal entries are recognized; all transaction types are
  direction-mapped; the full gate passes with six green checks.
- **Failure paths** (synthetic, no data needed): these are the important ones.
  They prove the gate FAILS when it should, so the gate is not just decorative:
  - `test_gate_fails_when_detail_disagrees_with_control` - keystone breaks.
  - `test_gate_fails_on_per_customer_mismatch` - aggregate ties but a customer
    row does not.
  - `test_gate_fails_when_open_invoices_gap_unexplained` - an Open Invoices
    shortfall that is NOT explained by post-period payments is rejected.
  - `test_garbage_money_quarantines_file_not_crashes` - a malformed money cell
    quarantines the row instead of being silently skipped.

### 4. Try breaking it yourself (recommended)

The fastest way to trust a reconciliation gate is to watch it fail. Copy a real
CSV, change one number in a customer row so it no longer ties, point the runner
at the altered folder, and confirm the gate reports FAIL with the delta. Then
restore the original and confirm it passes again.

## What this build adds over the first-session parser

- Three new report parsers (Summary, Detail, Open Invoices), not just Customer
  Balance Detail.
- The `General Journal` transaction type, which the first parser would have
  flagged as unknown. The entire `> 90` bucket is composed of legacy General
  Journal entries netting to -13,341.66; the build handles them correctly.
- The full six-check cross-report gate, including the Open Invoices quirk.
- Per-file encoding detection (three files are ASCII, one is cp1252).

## Honest scope notes

- This is the AR side of v1. The Cash side needs the GL extended through 05/31
  and a Balance Sheet, neither exported yet.
- The canonical database load (Stage 3) and the dbt marts (Stage 5) are not in
  this build; this is the parse-and-reconcile core that everything downstream
  depends on. Writing the verified records into Postgres is the next step.
- Aging is read from QuickBooks' own Aging Detail buckets, treated as the answer
  key. Computing buckets independently from Open Invoices due dates and terms is
  a v1 finishing task once the canonical load exists.
```
