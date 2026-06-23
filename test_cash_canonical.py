"""
Tests for the canonical cash load, the database-side cash gate, and the gated
cash mart refresh.

Needs a running Postgres and DATABASE_URL set, e.g.:
    docker compose up -d --wait
    $env:DATABASE_URL = "postgresql://qbd:qbd@localhost:5433/qbd"
    python -m pytest test_cash_canonical.py -v

Each fixture works in its own throwaway schema (cash_test_<hex>) so the dev data
in public is never touched. Read-only assertions share one module-scoped loaded
schema; tamper and refresh-block tests each get a fresh one.
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
from qbd_balance_sheet import parse_balance_sheet
from qbd_gl_cash import parse_general_ledger
import load_canonical as lc
import load_cash as lcash
import refresh_marts as rm

AR_DATA = Path("ips_files")
SUMMARY = AR_DATA / "IndustrialPipe_AR-Aging-Summary_2026-05-31.csv"
DETAIL = AR_DATA / "IndustrialPipe_AR-Aging-Detail_2026-05-31.csv"
OPENINV = AR_DATA / "IndustrialPipe_OpenInvoices _2026-05-31.csv"
CBD = AR_DATA / "IndustrialPipe_CustomerBalanceDetail_2026_05.csv"

CASH_DATA = Path("cash_files")
BS = CASH_DATA / "BS_May_31_2026.xlsx"
GL = CASH_DATA / "General_Ledger_April_May_2026.xlsx"

CASH_TOTAL = Decimal("451068.87")
AR_TOTAL = Decimal("609772.89")
AS_OF = date(2026, 5, 31)
TENANT = "ips"

pytestmark = [
    pytest.mark.skipif(
        "DATABASE_URL" not in os.environ,
        reason="DATABASE_URL not set; start the compose Postgres and set it"),
    pytest.mark.skipif(not BS.exists(), reason="real cash files not present in cash_files"),
]


def _new_schema_conn():
    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
    schema = "cash_test_" + uuid.uuid4().hex[:8]
    conn.execute(f'CREATE SCHEMA "{schema}"')
    conn.execute(f'SET search_path TO "{schema}"')
    lc.apply_schema(conn)
    return conn, schema


def _drop_schema(conn, schema):
    conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
    conn.close()


def _load_cash(conn):
    bs = parse_balance_sheet(BS)
    gl = parse_general_ledger(GL)
    results = lcash.load_cash_snapshot(conn, TENANT, AS_OF, bs, gl)
    return {k: v[0] for k, v in results.items()}


def _load_ar(conn):
    summary = reports.parse_aging_summary(SUMMARY)
    detail = reports.parse_aging_detail(DETAIL)
    oi = reports.parse_open_invoices(OPENINV)
    cbd = qcbd.parse_customer_balance_detail(CBD)
    results = lc.load_snapshot(conn, TENANT, AS_OF, summary, detail, oi, cbd)
    return {k: v[0] for k, v in results.items()}


@pytest.fixture(scope="module")
def cash_loaded():
    conn, schema = _new_schema_conn()
    ids = _load_cash(conn)
    yield conn, ids
    _drop_schema(conn, schema)


@pytest.fixture()
def fresh_cash():
    conn, schema = _new_schema_conn()
    ids = _load_cash(conn)
    yield conn, ids
    _drop_schema(conn, schema)


@pytest.fixture()
def full_loaded():
    conn, schema = _new_schema_conn()
    _load_ar(conn)
    ids = _load_cash(conn)
    yield conn, ids
    _drop_schema(conn, schema)


# --- happy path on the real cash batch --------------------------------------

def test_canonical_cash_total_ties(cash_loaded):
    conn, ids = cash_loaded
    assert lcash.canonical_cash_total(conn, TENANT, ids["general_ledger"]) == CASH_TOTAL


def test_cash_readback_gate_passes(cash_loaded):
    conn, ids = cash_loaded
    gate = lcash.run_canonical_cash_gate(conn, TENANT, ids["general_ledger"])
    assert gate.passed is True
    assert len(gate.checks) == 3
    assert gate.cash_total == CASH_TOTAL
    assert gate.cash_control == CASH_TOTAL


def test_full_gl_landed_only_cash_promoted(cash_loaded):
    conn, ids = cash_loaded
    gl_batch = ids["general_ledger"]
    landed = conn.execute(
        "SELECT count(*) FROM gl_raw_accounts WHERE tenant_id = %s AND batch_id = %s",
        (TENANT, gl_batch)).fetchone()[0]
    promoted = conn.execute(
        "SELECT count(*) FROM cash_accounts WHERE tenant_id = %s AND batch_id = %s",
        (TENANT, gl_batch)).fetchone()[0]
    flagged = conn.execute(
        "SELECT count(*) FROM gl_raw_accounts WHERE tenant_id = %s AND batch_id = %s AND is_cash",
        (TENANT, gl_batch)).fetchone()[0]
    assert landed > promoted
    assert promoted == 3
    assert flagged == 3


def test_cash_snapshot_row_written_and_ties(cash_loaded):
    conn, ids = cash_loaded
    row = conn.execute(
        "SELECT gl_cash_total, bs_cash_control, delta FROM cash_snapshots "
        "WHERE tenant_id = %s AND gl_batch_id = %s AND bs_batch_id = %s",
        (TENANT, ids["general_ledger"], ids["balance_sheet"])).fetchone()
    assert row is not None
    gl_total, bs_control, delta = row
    assert gl_total == CASH_TOTAL
    assert bs_control == CASH_TOTAL
    assert delta == Decimal("0")


def test_cash_accounts_carry_bs_balance(cash_loaded):
    conn, ids = cash_loaded
    rows = conn.execute(
        "SELECT account_number, ending_balance, bs_balance FROM cash_accounts "
        "WHERE tenant_id = %s AND batch_id = %s ORDER BY account_number",
        (TENANT, ids["general_ledger"])).fetchall()
    assert {r[0] for r in rows} == {"10000", "10050", "10100"}
    for _, ending, bs_balance in rows:
        assert ending == bs_balance


def test_idempotent_cash_reload_is_noop(cash_loaded):
    conn, _ = cash_loaded
    bs = parse_balance_sheet(BS)
    gl = parse_general_ledger(GL)
    results = lcash.load_cash_snapshot(conn, TENANT, AS_OF, bs, gl)
    assert all(flag is False for _, flag in results.values())
    snaps = conn.execute(
        "SELECT count(*) FROM cash_snapshots WHERE tenant_id = %s", (TENANT,)).fetchone()[0]
    assert snaps == 1


# --- failure paths: the canonical cash layer must detect tampering ----------

def test_tampered_cash_ending_fails_gate(fresh_cash):
    conn, ids = fresh_cash
    conn.execute(
        "UPDATE cash_accounts SET ending_balance = ending_balance + 100.00 "
        "WHERE tenant_id = %s AND batch_id = %s AND account_number = '10000'",
        (TENANT, ids["general_ledger"]))
    gate = lcash.run_canonical_cash_gate(conn, TENANT, ids["general_ledger"])
    assert gate.passed is False


# --- the gated cash mart refresh --------------------------------------------

def test_refresh_populates_cash_marts(full_loaded):
    conn, _ = full_loaded
    assert rm.run_refresh(conn, TENANT) == 0
    row = conn.execute(
        "SELECT total_cash, bs_cash_control, account_count, recon_delta, gate_passed "
        "FROM mart_cash_summary WHERE tenant_id = %s", (TENANT,)).fetchone()
    assert row is not None
    total_cash, bs_control, account_count, recon_delta, gate_passed = row
    assert total_cash == CASH_TOTAL
    assert bs_control == CASH_TOTAL
    assert account_count == 3
    assert recon_delta == Decimal("0")
    assert gate_passed is True
    # the per-account breakdown mart is populated and ties
    per_acct = conn.execute(
        "SELECT count(*), SUM(ending_balance) FROM mart_cash_accounts WHERE tenant_id = %s",
        (TENANT,)).fetchone()
    assert per_acct[0] == 3
    assert per_acct[1] == CASH_TOTAL


def test_failure_path_blocked_refresh_preserves_cash_marts(full_loaded):
    conn, ids = full_loaded
    assert rm.run_refresh(conn, TENANT) == 0
    before = conn.execute("SELECT * FROM mart_cash_summary WHERE tenant_id = %s",
                          (TENANT,)).fetchall()
    before_stamps = conn.execute(
        "SELECT count(*) FROM mart_cash_refresh_stamps WHERE tenant_id = %s",
        (TENANT,)).fetchone()[0]
    # a tampered cash ending must block the entire refresh
    conn.execute(
        "UPDATE cash_accounts SET ending_balance = ending_balance + 100.00 "
        "WHERE tenant_id = %s AND batch_id = %s AND account_number = '10050'",
        (TENANT, ids["general_ledger"]))
    assert rm.run_refresh(conn, TENANT) == 2
    after = conn.execute("SELECT * FROM mart_cash_summary WHERE tenant_id = %s",
                         (TENANT,)).fetchall()
    after_stamps = conn.execute(
        "SELECT count(*) FROM mart_cash_refresh_stamps WHERE tenant_id = %s",
        (TENANT,)).fetchone()[0]
    assert after == before
    assert after_stamps == before_stamps
