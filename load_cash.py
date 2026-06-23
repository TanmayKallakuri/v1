"""
load_cash.py - canonical Postgres load and database-side gate for the Cash side.

Mirrors load_canonical.py for AR. Lands the two cash source files (Balance Sheet
and General Ledger) into the canonical schema, one batch per file, in a single
transaction, with the same idempotency (per-file sha256 no-op). The full GL is
landed raw for audit (gl_raw_accounts); only the in-scope cash accounts are
promoted to canonical (cash_accounts). It then proves the database stands on its
own: the cash gate is rebuilt purely from Postgres rows and fed to
cash_reconcile.reconcile_cash unmodified.

Usage:
    python load_cash.py [directory] --tenant ips --as-of 2026-05-31

Requires DATABASE_URL, e.g. postgresql://qbd:qbd@localhost:5433/qbd
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg

from qbd_balance_sheet import parse_balance_sheet
from qbd_gl_cash import parse_general_ledger
from cash_reconcile import CashReconReport, reconcile_cash
from load_canonical import (
    LoadError,
    _existing_batch,
    _insert_batch,
    _require_clean,
    apply_schema,
    as_money,
    get_conn,
)

QUANT4 = Decimal("0.0001")


# --- load --------------------------------------------------------------------

def _load_balance_sheet_batch(conn, tenant_id: str, as_of_date: date, bs) -> tuple[uuid.UUID, bool]:
    _require_clean("balance sheet", bs.quarantine)
    existing = _existing_batch(conn, tenant_id, bs.source_sha256)
    if existing is not None:
        return existing, False
    batch_id = uuid.uuid4()
    # The Balance Sheet's role in v1 is the cash control; its per-account cash
    # figures are stored on cash_accounts and its control is in cash_snapshots.
    # The batch row is the Balance Sheet's immutable provenance record.
    _insert_batch(conn, batch_id, tenant_id, Path(bs.source_path).name,
                  "balance_sheet", as_of_date, bs.source_sha256,
                  bs.encoding, len(bs.accounts))
    return batch_id, True


def _load_gl_cash_batch(conn, tenant_id: str, as_of_date: date, bs, gl) -> tuple[uuid.UUID, bool]:
    existing = _existing_batch(conn, tenant_id, gl.source_sha256)
    if existing is not None:
        return existing, False
    batch_id = uuid.uuid4()
    _insert_batch(conn, batch_id, tenant_id, Path(gl.source_path).name,
                  "general_ledger", as_of_date, gl.source_sha256,
                  gl.encoding, len(gl.cash))

    # raw landing: every GL account header, cash or not, for audit
    for acc in gl.all_accounts:
        conn.execute(
            "INSERT INTO gl_raw_accounts "
            "(tenant_id, batch_id, account_number, account_name, opening_balance, is_cash, as_of_date) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (tenant_id, batch_id, acc.number, acc.name,
             as_money(acc.opening, f"gl opening {acc.number}"), acc.is_cash, as_of_date),
        )

    # canonical promotion: cash accounts only, with the Balance Sheet figure
    # stored alongside for an auditable per-account tie-out.
    bs_by = bs.cash_by_account()
    for num in sorted(gl.cash):
        a = gl.cash[num]
        bs_amt = bs_by.get(num)
        conn.execute(
            "INSERT INTO cash_accounts "
            "(tenant_id, batch_id, account_number, account_name, opening_balance, "
            "ending_balance, txn_count, bs_balance, as_of_date) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (tenant_id, batch_id, num, a.name,
             as_money(a.opening, f"cash opening {num}"),
             as_money(a.ending, f"cash ending {num}"),
             len(a.txns),
             as_money(bs_amt, f"cash bs balance {num}") if bs_amt is not None else None,
             as_of_date),
        )
    return batch_id, True


def load_cash_snapshot(conn, tenant_id: str, as_of_date: date, bs, gl) -> dict:
    """Load the Balance Sheet and GL cash batches atomically and write the cash
    reconciliation snapshot. Any LoadError rolls back everything."""
    with conn.transaction():
        bs_batch_id, bs_loaded = _load_balance_sheet_batch(conn, tenant_id, as_of_date, bs)
        gl_batch_id, gl_loaded = _load_gl_cash_batch(conn, tenant_id, as_of_date, bs, gl)

        existing = conn.execute(
            "SELECT 1 FROM cash_snapshots "
            "WHERE tenant_id = %s AND gl_batch_id = %s AND bs_batch_id = %s",
            (tenant_id, gl_batch_id, bs_batch_id),
        ).fetchone()
        if existing is None:
            gl_total = canonical_cash_total(conn, tenant_id, gl_batch_id)
            bs_control = as_money(bs.cash_control_total(), "balance sheet cash control")
            conn.execute(
                "INSERT INTO cash_snapshots "
                "(tenant_id, gl_batch_id, bs_batch_id, as_of_date, gl_cash_total, bs_cash_control) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (tenant_id, gl_batch_id, bs_batch_id, as_of_date, gl_total, bs_control),
            )
    return {"general_ledger": (gl_batch_id, gl_loaded),
            "balance_sheet": (bs_batch_id, bs_loaded)}


# --- read-back: rebuild the cash gate's inputs purely from Postgres ----------

def canonical_cash_total(conn, tenant_id: str, gl_batch_id) -> Decimal:
    total = conn.execute(
        "SELECT COALESCE(SUM(ending_balance), 0) FROM cash_accounts "
        "WHERE tenant_id = %s AND batch_id = %s",
        (tenant_id, gl_batch_id),
    ).fetchone()[0]
    return as_money(total, "canonical cash total")


def run_canonical_cash_gate(conn, tenant_id: str, gl_batch_id) -> CashReconReport:
    """Rebuild the per-account GL endings and Balance Sheet balances from the
    canonical cash_accounts rows and run the cash gate, unmodified."""
    rows = conn.execute(
        "SELECT account_number, ending_balance, bs_balance FROM cash_accounts "
        "WHERE tenant_id = %s AND batch_id = %s",
        (tenant_id, gl_batch_id),
    ).fetchall()
    if not rows:
        raise LoadError("missing_cash_batch",
                        f"no cash accounts for tenant {tenant_id!r} batch {gl_batch_id}")
    gl_by = {num: as_money(ending, f"cash ending {num}") for num, ending, _ in rows}
    bs_by = {num: as_money(bs, f"cash bs balance {num}")
             for num, _, bs in rows if bs is not None}
    return reconcile_cash(gl_by, bs_by)


# --- CLI ---------------------------------------------------------------------

def _find(directory: Path, *needles: str) -> Path | None:
    for p in sorted(directory.glob("*.xlsx")):
        name = p.name.lower()
        if all(n.lower() in name for n in needles):
            return p
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Load the cash batch into canonical Postgres and rerun the cash gate from the database")
    parser.add_argument("directory", nargs="?", default="cash_files",
                        help="directory containing the Balance Sheet and General Ledger xlsx")
    parser.add_argument("--tenant", default="ips")
    parser.add_argument("--as-of", dest="as_of", default="2026-05-31",
                        help="snapshot as-of date, YYYY-MM-DD")
    args = parser.parse_args(argv)

    try:
        as_of = date.fromisoformat(args.as_of)
    except ValueError:
        print(f"invalid as-of date {args.as_of!r}, expected YYYY-MM-DD")
        return 1

    directory = Path(args.directory)
    bs_p = _find(directory, "bs") or _find(directory, "balance")
    gl_p = _find(directory, "general", "ledger") or _find(directory, "gl")
    missing = [label for label, p in [("Balance Sheet", bs_p), ("General Ledger", gl_p)] if p is None]
    if missing:
        print(f"Missing cash source files in {directory}: {', '.join(missing)}")
        return 1

    bs = parse_balance_sheet(bs_p)
    gl = parse_general_ledger(gl_p)

    try:
        conn = get_conn()
    except LoadError as exc:
        print(str(exc))
        return 1
    except psycopg.OperationalError:
        print("could not connect to Postgres; check that the database is up "
              "and DATABASE_URL points at it")
        return 1

    with conn:
        apply_schema(conn)
        try:
            results = load_cash_snapshot(conn, args.tenant, as_of, bs, gl)
        except LoadError as exc:
            print(f"LOAD REFUSED: {exc}")
            return 1

        gl_batch_id = results["general_ledger"][0]
        bs_batch_id = results["balance_sheet"][0]
        gate = run_canonical_cash_gate(conn, args.tenant, gl_batch_id)
        computed = canonical_cash_total(conn, args.tenant, gl_batch_id)

        bar = "=" * 70
        print(bar)
        print("CANONICAL CASH RECONCILIATION REPORT")
        print(bar)
        for report_type, (batch_id, was_loaded) in results.items():
            tag = "loaded" if was_loaded else "no-op (already loaded)"
            print(f"  {report_type:16s} {tag}")
            print(f"         batch {batch_id}")
        promoted = conn.execute(
            "SELECT count(*) FROM cash_accounts WHERE tenant_id = %s AND batch_id = %s",
            (args.tenant, gl_batch_id)).fetchone()[0]
        landed = conn.execute(
            "SELECT count(*) FROM gl_raw_accounts WHERE tenant_id = %s AND batch_id = %s",
            (args.tenant, gl_batch_id)).fetchone()[0]
        print(bar)
        print(f"GL accounts landed (raw, for audit): {landed}")
        print(f"Cash accounts promoted to canonical: {promoted}")
        print(f"GL-computed cash total (cash_accounts): {computed.quantize(QUANT4)}")
        print(f"Balance Sheet cash control: {gate.cash_control}")
        print(bar)
        for check in gate.checks:
            status = "PASS" if check.passed else "FAIL"
            dz = f"  (delta {check.delta})" if check.delta is not None else ""
            print(f"  [{status}] {check.name}")
            print(f"         {check.detail}{dz}")
        print(bar)
        verdict = ("CANONICAL CASH GATE PASSED - dashboard may refresh" if gate.passed
                   else "CANONICAL CASH GATE FAILED - refresh blocked")
        print(verdict)
        print(bar)
        return 0 if gate.passed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
