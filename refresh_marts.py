"""
refresh_marts.py - Stage 5: gated refresh of the serving marts (gold layer).

Applies the mart definitions (marts.sql), picks the latest complete snapshot
per tenant, reruns the six-check reconciliation gate from the canonical tables,
and only then refreshes the five materialized views and writes a refresh stamp,
all in one transaction.

apply_marts runs before the gate, but it is idempotent definition-only DDL
(CREATE TABLE IF NOT EXISTS, CREATE OR REPLACE VIEW, CREATE MATERIALIZED VIEW
IF NOT EXISTS ... WITH NO DATA). That does not weaken the blocking guarantee:
definitions carry no snapshot data, matviews change content only on REFRESH,
and every REFRESH plus the stamp insert happens after the gate passes. A failed
gate means zero writes, so the last good snapshot stays live.

Usage:
    python refresh_marts.py --tenant ips

Requires DATABASE_URL, e.g. postgresql://qbd:qbd@localhost:5433/qbd
Exit codes: 0 refreshed, 1 no complete snapshot or environment problem,
2 reconciliation gate failed (refresh blocked).
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

import psycopg

from load_canonical import LoadError, canonical_ar_total, get_conn, run_canonical_gate
from load_cash import canonical_cash_total, run_canonical_cash_gate

MARTS_PATH = Path(__file__).with_name("marts.sql")
QUANT4 = Decimal("0.0001")
REPORT_TYPES = ("aging_summary", "aging_detail", "open_invoices", "customer_balance_detail")
REFRESH_ORDER = ("mart_ar_aging_by_customer", "mart_top_overdue", "mart_ar_summary",
                 "mart_credits_unapplied", "mart_ar_trend")
CASH_REFRESH_ORDER = ("mart_cash_accounts", "mart_cash_summary")
BLOCKED_MESSAGE = ("MART REFRESH BLOCKED: reconciliation gate failed; "
                   "last good snapshot stays live")


def apply_marts(conn: psycopg.Connection) -> None:
    with conn.transaction():
        conn.execute(MARTS_PATH.read_text(encoding="ascii"))


def current_batches(conn: psycopg.Connection, tenant: str) -> dict | None:
    """The latest complete snapshot for the tenant, or None if there is none.

    Returns {"as_of_date": date, "batch_ids": {report_type: batch_id}} with the
    batch_ids dict keyed exactly as run_canonical_gate expects.
    """
    rows = conn.execute(
        "SELECT report_type, batch_id, as_of_date FROM mart_current_batches "
        "WHERE tenant_id = %s",
        (tenant,),
    ).fetchall()
    batch_ids = {report_type: batch_id for report_type, batch_id, _ in rows}
    if len(rows) < 4 or set(batch_ids) != set(REPORT_TYPES):
        return None
    return {"as_of_date": rows[0][2], "batch_ids": batch_ids}


def current_cash_batch(conn: psycopg.Connection, tenant: str) -> dict | None:
    """The latest cash snapshot for the tenant, or None if cash is not loaded.

    Cash is optional: a tenant may have AR loaded before cash arrives. When a
    cash snapshot is present it is gated and refreshed alongside AR; a failing
    cash gate blocks the entire refresh, exactly like a failing AR gate.
    """
    row = conn.execute(
        "SELECT as_of_date, gl_batch_id, bs_batch_id FROM mart_current_cash_batch "
        "WHERE tenant_id = %s",
        (tenant,),
    ).fetchone()
    if row is None:
        return None
    return {"as_of_date": row[0], "gl_batch_id": row[1], "bs_batch_id": row[2]}


def run_refresh(conn: psycopg.Connection, tenant: str) -> int:
    apply_marts(conn)

    current = current_batches(conn, tenant)
    if current is None:
        print(f"MART REFRESH SKIPPED: no complete loaded snapshot for tenant {tenant!r}; "
              "need all four report types loaded for one as-of date")
        return 1
    as_of = current["as_of_date"]
    batch_ids = current["batch_ids"]

    try:
        gate = run_canonical_gate(conn, tenant, batch_ids)
        computed = canonical_ar_total(conn, tenant, batch_ids["aging_detail"])
    except LoadError as exc:
        print(BLOCKED_MESSAGE)
        print(f"  [FAIL] canonical read-back: {exc}")
        return 2

    if not gate.passed:
        print(BLOCKED_MESSAGE)
        for check in gate.checks:
            if not check.passed:
                print(f"  [FAIL] {check.name}: {check.detail}")
        return 2

    # Cash is gated too when it is loaded. Both gates must pass before any write;
    # a failing cash gate blocks the entire refresh and keeps the last good
    # snapshot live, the same hard stop as AR.
    cash = current_cash_batch(conn, tenant)
    cash_gate = None
    cash_computed = None
    if cash is not None:
        try:
            cash_gate = run_canonical_cash_gate(conn, tenant, cash["gl_batch_id"])
            cash_computed = canonical_cash_total(conn, tenant, cash["gl_batch_id"])
        except LoadError as exc:
            print(BLOCKED_MESSAGE)
            print(f"  [FAIL] canonical cash read-back: {exc}")
            return 2
        if not cash_gate.passed:
            print(BLOCKED_MESSAGE)
            for check in cash_gate.checks:
                if not check.passed:
                    print(f"  [FAIL] cash {check.name}: {check.detail}")
            return 2

    checks_passed = sum(1 for c in gate.checks if c.passed)
    checks_total = len(gate.checks)

    with conn.transaction():
        stamp_id, refreshed_at, delta = conn.execute(
            "INSERT INTO mart_refresh_stamps "
            "(tenant_id, as_of_date, control_total, computed_total, gate_passed, "
            "checks_passed, checks_total, summary_batch_id, detail_batch_id, "
            "open_invoices_batch_id, cbd_batch_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "RETURNING stamp_id, refreshed_at, delta",
            (tenant, as_of, gate.ar_total, computed, True, checks_passed, checks_total,
             batch_ids["aging_summary"], batch_ids["aging_detail"],
             batch_ids["open_invoices"], batch_ids["customer_balance_detail"]),
        ).fetchone()
        for mart in REFRESH_ORDER:
            # plain REFRESH only: CONCURRENTLY is illegal inside a transaction block
            conn.execute(f"REFRESH MATERIALIZED VIEW {mart}")

        cash_refreshed_at = None
        cash_delta = None
        if cash is not None:
            cash_checks_passed = sum(1 for c in cash_gate.checks if c.passed)
            cash_checks_total = len(cash_gate.checks)
            cash_refreshed_at, cash_delta = conn.execute(
                "INSERT INTO mart_cash_refresh_stamps "
                "(tenant_id, as_of_date, control_total, computed_total, gate_passed, "
                "checks_passed, checks_total, gl_batch_id, bs_batch_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING refreshed_at, delta",
                (tenant, cash["as_of_date"], cash_gate.cash_control, cash_computed, True,
                 cash_checks_passed, cash_checks_total,
                 cash["gl_batch_id"], cash["bs_batch_id"]),
            ).fetchone()
            for mart in CASH_REFRESH_ORDER:
                conn.execute(f"REFRESH MATERIALIZED VIEW {mart}")

    print("MART REFRESH STAMP")
    print(f"  tenant={tenant} as_of={as_of} refreshed_at={refreshed_at}")
    print(f"  control_total={gate.ar_total.quantize(QUANT4)} "
          f"computed_total={computed.quantize(QUANT4)} delta={delta} "
          f"gate=PASSED checks={checks_passed}/{checks_total}")
    if cash is not None:
        cash_checks_passed = sum(1 for c in cash_gate.checks if c.passed)
        cash_checks_total = len(cash_gate.checks)
        print("CASH MART REFRESH STAMP")
        print(f"  tenant={tenant} as_of={cash['as_of_date']} refreshed_at={cash_refreshed_at}")
        print(f"  control_total={cash_gate.cash_control.quantize(QUANT4)} "
              f"computed_total={cash_computed.quantize(QUANT4)} delta={cash_delta} "
              f"gate=PASSED checks={cash_checks_passed}/{cash_checks_total}")

    summary_row = conn.execute(
        "SELECT total_ar, bucket_current, bucket_1_30, bucket_31_60, "
        "bucket_61_90, bucket_over_90, recon_delta "
        "FROM mart_ar_summary WHERE tenant_id = %s",
        (tenant,),
    ).fetchone()
    print("mart_ar_summary:")
    if summary_row is None:
        print(f"  no row for tenant {tenant}")
    else:
        total_ar, cur, d1_30, d31_60, d61_90, over_90, recon_delta = summary_row
        print(f"  total_ar={total_ar} current={cur} 1-30={d1_30} 31-60={d31_60} "
              f"61-90={d61_90} over_90={over_90} recon_delta={recon_delta}")

    if cash is not None:
        cash_row = conn.execute(
            "SELECT total_cash, bs_cash_control, account_count, recon_delta "
            "FROM mart_cash_summary WHERE tenant_id = %s",
            (tenant,),
        ).fetchone()
        print("mart_cash_summary:")
        if cash_row is None:
            print(f"  no row for tenant {tenant}")
        else:
            total_cash, bs_control, account_count, cash_recon_delta = cash_row
            print(f"  total_cash={total_cash} bs_cash_control={bs_control} "
                  f"accounts={account_count} recon_delta={cash_recon_delta}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Refresh the serving marts behind the reconciliation gate")
    parser.add_argument("--tenant", default="ips")
    args = parser.parse_args(argv)

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
        return run_refresh(conn, args.tenant)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
