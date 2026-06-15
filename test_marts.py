"""
Tests for the serving marts and the gated refresh (Stage 5).

Needs a running Postgres and DATABASE_URL set, e.g.:
    docker compose up -d --wait
    $env:DATABASE_URL = "postgresql://qbd:qbd@localhost:5433/qbd"
    python -m pytest test_marts.py -v

Each fixture works in its own throwaway schema (mart_test_<hex>) so the dev
data in public is never touched. The marts are created by run_refresh on the
schema-scoped connection, which is what binds them to the test schema's
canonical tables. Read-only assertions share one module-scoped refreshed
schema; the failure-path test gets a fresh one.
"""

import os
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

import qbd_reports as reports
import qbd_cbd as qcbd
import load_canonical as lc
import refresh_marts as rm

DATA = Path("ips_files")
SUMMARY = DATA / "IndustrialPipe_AR-Aging-Summary_2026-05-31.csv"
DETAIL = DATA / "IndustrialPipe_AR-Aging-Detail_2026-05-31.csv"
OPENINV = DATA / "IndustrialPipe_OpenInvoices _2026-05-31.csv"
CBD = DATA / "IndustrialPipe_CustomerBalanceDetail_2026_05.csv"

AR_TOTAL = Decimal("609772.89")
AS_OF = date(2026, 5, 31)
TENANT = "ips"
SPRINKLER_GJ = Decimal("-13341.66")
MARTS = ("mart_ar_aging_by_customer", "mart_top_overdue", "mart_ar_summary",
         "mart_credits_unapplied", "mart_ar_trend")

MONEY_COLUMNS = {
    "mart_ar_aging_by_customer": ("bucket_current", "bucket_1_30", "bucket_31_60",
                                  "bucket_61_90", "bucket_over_90", "total",
                                  "overdue_31_plus"),
    "mart_top_overdue": ("bucket_31_60", "bucket_61_90", "bucket_over_90",
                         "total", "overdue_31_plus"),
    "mart_ar_summary": ("total_ar", "bucket_current", "bucket_1_30", "bucket_31_60",
                        "bucket_61_90", "bucket_over_90", "recon_delta"),
    "mart_credits_unapplied": ("signed_amount",),
    "mart_ar_trend": ("total_ar", "bucket_current", "bucket_1_30", "bucket_31_60",
                      "bucket_61_90", "bucket_over_90", "prev_total_ar",
                      "total_ar_change", "bucket_current_change", "bucket_1_30_change",
                      "bucket_31_60_change", "bucket_61_90_change",
                      "bucket_over_90_change"),
    "mart_refresh_stamps": ("control_total", "computed_total", "delta"),
}

pytestmark = [
    pytest.mark.skipif(
        "DATABASE_URL" not in os.environ,
        reason="DATABASE_URL not set; start the compose Postgres and set it"),
    pytest.mark.skipif(not SUMMARY.exists(), reason="real files not present in ips_files"),
]


def _new_schema_conn():
    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
    schema = "mart_test_" + uuid.uuid4().hex[:8]
    conn.execute(f'CREATE SCHEMA "{schema}"')
    conn.execute(f'SET search_path TO "{schema}"')
    lc.apply_schema(conn)
    return conn, schema


def _drop_schema(conn, schema):
    conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
    conn.close()


def _load_real(conn):
    summary = reports.parse_aging_summary(SUMMARY)
    detail = reports.parse_aging_detail(DETAIL)
    oi = reports.parse_open_invoices(OPENINV)
    cbd = qcbd.parse_customer_balance_detail(CBD)
    results = lc.load_snapshot(conn, TENANT, AS_OF, summary, detail, oi, cbd)
    return {k: v[0] for k, v in results.items()}


def _snapshot_marts(conn):
    out = {}
    for table in MARTS + ("mart_refresh_stamps",):
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        out[table] = sorted(rows, key=repr)
    return out


@pytest.fixture(scope="module")
def refreshed():
    conn, schema = _new_schema_conn()
    ids = _load_real(conn)
    assert rm.run_refresh(conn, TENANT) == 0
    yield conn, ids
    _drop_schema(conn, schema)


@pytest.fixture()
def fresh_refreshed():
    conn, schema = _new_schema_conn()
    ids = _load_real(conn)
    yield conn, ids
    _drop_schema(conn, schema)


def test_summary_total_and_delta(refreshed):
    conn, _ = refreshed
    rows = conn.execute(
        "SELECT total_ar, recon_delta, gate_passed, as_of_date "
        "FROM mart_ar_summary WHERE tenant_id = %s", (TENANT,)).fetchall()
    assert len(rows) == 1
    total_ar, recon_delta, gate_passed, as_of = rows[0]
    assert total_ar == AR_TOTAL
    assert recon_delta == Decimal("0")
    assert gate_passed is True
    assert as_of == AS_OF


def test_summary_bucket_totals(refreshed):
    conn, _ = refreshed
    row = conn.execute(
        "SELECT bucket_current, bucket_1_30, bucket_31_60, bucket_61_90, bucket_over_90 "
        "FROM mart_ar_summary WHERE tenant_id = %s", (TENANT,)).fetchone()
    assert row == (Decimal("86755.84"), Decimal("377971.14"), Decimal("134397.12"),
                   Decimal("23990.45"), Decimal("-13341.66"))


def test_by_customer_totals_sum_to_control(refreshed):
    conn, _ = refreshed
    count, total = conn.execute(
        "SELECT count(*), SUM(total) FROM mart_ar_aging_by_customer "
        "WHERE tenant_id = %s", (TENANT,)).fetchone()
    assert count > 0
    assert total == AR_TOTAL


def test_credits_unapplied_contains_aa_sprinkler_gj(refreshed):
    conn, _ = refreshed
    gj = conn.execute(
        "SELECT signed_amount FROM mart_credits_unapplied "
        "WHERE tenant_id = %s AND source = 'unapplied_item' "
        "AND txn_type = 'General Journal' AND raw_name = 'A & A Sprinkler'",
        (TENANT,)).fetchall()
    assert len(gj) == 1
    assert gj[0][0] == SPRINKLER_GJ
    neg = conn.execute(
        "SELECT signed_amount FROM mart_credits_unapplied "
        "WHERE tenant_id = %s AND source = 'negative_balance' "
        "AND raw_name = 'A & A Sprinkler'", (TENANT,)).fetchall()
    assert len(neg) == 1
    assert neg[0][0] == SPRINKLER_GJ


def test_top_overdue_is_deterministic_and_positive(refreshed):
    conn, _ = refreshed
    rows = conn.execute(
        "SELECT overdue_rank, overdue_31_plus, raw_name FROM mart_top_overdue "
        "WHERE tenant_id = %s ORDER BY overdue_rank", (TENANT,)).fetchall()
    assert 0 < len(rows) <= 10
    ranks = [r[0] for r in rows]
    assert ranks == list(range(1, len(rows) + 1))
    amounts = [r[1] for r in rows]
    assert all(a > 0 for a in amounts)
    assert all(amounts[i] >= amounts[i + 1] for i in range(len(amounts) - 1))
    assert "A & A Sprinkler" not in {r[2] for r in rows}


def test_trend_single_row_with_null_prev(refreshed):
    conn, _ = refreshed
    rows = conn.execute(
        "SELECT as_of_date, total_ar, prev_total_ar, total_ar_change, "
        "bucket_current_change, bucket_1_30_change, bucket_31_60_change, "
        "bucket_61_90_change, bucket_over_90_change "
        "FROM mart_ar_trend WHERE tenant_id = %s", (TENANT,)).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row[0] == AS_OF
    assert row[1] == AR_TOTAL
    assert all(v is None for v in row[2:])


def test_failure_path_blocked_refresh_preserves_marts(fresh_refreshed):
    conn, ids = fresh_refreshed
    assert rm.run_refresh(conn, TENANT) == 0
    before = _snapshot_marts(conn)
    conn.execute(
        "UPDATE ar_transactions SET amount = amount + 100.00 WHERE txn_id = ("
        "SELECT min(txn_id) FROM ar_transactions "
        "WHERE tenant_id = %s AND batch_id = %s AND direction = 'increase')",
        (TENANT, ids["aging_detail"]))
    assert rm.run_refresh(conn, TENANT) == 2
    after = _snapshot_marts(conn)
    assert after == before


def test_mart_money_columns_are_numeric_19_4(refreshed):
    conn, _ = refreshed
    for table, columns in MONEY_COLUMNS.items():
        for column in columns:
            row = conn.execute(
                "SELECT format_type(a.atttypid, a.atttypmod) "
                "FROM pg_attribute a "
                "JOIN pg_class c ON c.oid = a.attrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = current_schema() "
                "AND c.relname = %s AND a.attname = %s",
                (table, column)).fetchone()
            assert row is not None, f"{table}.{column} not found"
            assert row[0] == "numeric(19,4)", f"{table}.{column} is {row[0]}"
