"""
Tests for the canonical Postgres load and database-side gate (Stage 3).

Needs a running Postgres and DATABASE_URL set, e.g.:
    docker compose up -d --wait
    $env:DATABASE_URL = "postgresql://qbd:qbd@localhost:5433/qbd"
    python -m pytest test_canonical.py -v

Each fixture works in its own throwaway schema (canon_test_<hex>) so the dev
data in public is never touched. Read-only assertions share one module-scoped
loaded schema; tamper and refusal tests each get a fresh one.
"""

import os
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

import qbd_core as core
import qbd_reports as reports
import qbd_cbd as qcbd
import load_canonical as lc

DATA = Path("ips_files")
SUMMARY = DATA / "IndustrialPipe_AR-Aging-Summary_2026-05-31.csv"
DETAIL = DATA / "IndustrialPipe_AR-Aging-Detail_2026-05-31.csv"
OPENINV = DATA / "IndustrialPipe_OpenInvoices _2026-05-31.csv"
CBD = DATA / "IndustrialPipe_CustomerBalanceDetail_2026_05.csv"

AR_TOTAL = Decimal("609772.89")
AS_OF = date(2026, 5, 31)
TENANT = "ips"
TABLES = ("batches", "customers", "invoices", "ar_transactions", "ar_aging_snapshots")
ZERO = Decimal("0")

pytestmark = [
    pytest.mark.skipif(
        "DATABASE_URL" not in os.environ,
        reason="DATABASE_URL not set; start the compose Postgres and set it"),
    pytest.mark.skipif(not SUMMARY.exists(), reason="real files not present in ips_files"),
]


def _new_schema_conn():
    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
    schema = "canon_test_" + uuid.uuid4().hex[:8]
    conn.execute(f'CREATE SCHEMA "{schema}"')
    conn.execute(f'SET search_path TO "{schema}"')
    lc.apply_schema(conn)
    return conn, schema


def _drop_schema(conn, schema):
    conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
    conn.close()


def _parse_all():
    return (reports.parse_aging_summary(SUMMARY),
            reports.parse_aging_detail(DETAIL),
            reports.parse_open_invoices(OPENINV),
            qcbd.parse_customer_balance_detail(CBD))


def _load_real(conn):
    summary, detail, oi, cbd = _parse_all()
    results = lc.load_snapshot(conn, TENANT, AS_OF, summary, detail, oi, cbd)
    return {k: v[0] for k, v in results.items()}


def _counts(conn):
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}


@pytest.fixture(scope="module")
def loaded():
    conn, schema = _new_schema_conn()
    ids = _load_real(conn)
    yield conn, ids
    _drop_schema(conn, schema)


@pytest.fixture()
def fresh_loaded():
    conn, schema = _new_schema_conn()
    ids = _load_real(conn)
    yield conn, ids
    _drop_schema(conn, schema)


@pytest.fixture()
def empty_schema():
    conn, schema = _new_schema_conn()
    yield conn
    _drop_schema(conn, schema)


# --- happy path on the real batch -------------------------------------------

def test_canonical_ar_total_ties(loaded):
    conn, ids = loaded
    assert lc.canonical_ar_total(conn, TENANT, ids["aging_detail"]) == AR_TOTAL


def test_full_readback_gate_passes(loaded):
    conn, ids = loaded
    gate = lc.run_canonical_gate(conn, TENANT, ids)
    assert gate.passed is True
    assert len(gate.checks) == 6
    assert gate.ar_total == AR_TOTAL


def test_general_journal_rows_and_signed_sum(loaded):
    conn, ids = loaded
    rows = conn.execute(
        "SELECT direction, amount FROM ar_transactions "
        "WHERE tenant_id = %s AND batch_id = %s AND txn_type = 'General Journal'",
        (TENANT, ids["aging_detail"])).fetchall()
    assert len(rows) == 31
    signed = sum((amt if d == "increase" else -amt for d, amt in rows), start=ZERO)
    assert signed == Decimal("-13341.66")


def test_derived_bucket_matches_parser_for_every_real_item(loaded):
    detail = reports.parse_aging_detail(DETAIL)
    assert len(detail.open_items) > 0
    for item in detail.open_items:
        derived = lc.derive_bucket(AS_OF, item.txn_date, item.due_date)
        assert derived == item.bucket, (
            f"row {item.source_row_index} ({item.txn_type} {item.num}): "
            f"derived {derived!r} vs parser {item.bucket!r}")


def test_idempotent_reload_is_noop(loaded):
    conn, ids = loaded
    before = _counts(conn)
    summary, detail, oi, cbd = _parse_all()
    results = lc.load_snapshot(conn, TENANT, AS_OF, summary, detail, oi, cbd)
    assert all(flag is False for _, flag in results.values())
    assert {k: v[0] for k, v in results.items()} == ids
    assert _counts(conn) == before


def test_all_invoice_deltas_are_zero(loaded):
    conn, ids = loaded
    total = conn.execute(
        "SELECT count(*) FROM invoices WHERE tenant_id = %s AND batch_id = %s",
        (TENANT, ids["aging_detail"])).fetchone()[0]
    bad = conn.execute(
        "SELECT count(*) FROM invoices WHERE tenant_id = %s AND batch_id = %s AND delta <> 0",
        (TENANT, ids["aging_detail"])).fetchone()[0]
    assert total > 0
    assert bad == 0


def test_no_invoice_number_collision_between_types():
    # pins the data contract behind invoices.computed_balance: within the real
    # detail batch no (customer, invoice number) pair is shared across types
    detail = reports.parse_aging_detail(DETAIL)
    invoice_keys = {(it.customer_key, it.num) for it in detail.open_items
                    if it.txn_type == "Invoice"}
    other_keys = {(it.customer_key, it.num) for it in detail.open_items
                  if it.txn_type != "Invoice"}
    assert invoice_keys
    assert other_keys
    assert invoice_keys & other_keys == set()


def test_cbd_raw_name_fallback_only_fires_for_zero_balance_customers():
    # pins the raw-name guarantee: every CBD customer with a nonzero ending was
    # already created with its true raw name by an earlier report; the real
    # batch has 20 CBD-only customers and all of them carry exactly zero
    summary, detail, oi, cbd = _parse_all()
    earlier = {row.customer_key for row in summary.summary_rows}
    earlier |= {item.customer_key for item in detail.open_items}
    earlier |= {item.customer_key for item in oi.open_items}
    cbd_only = {key: ending for key, ending in cbd.endings.items() if key not in earlier}
    assert len(cbd_only) == 20
    assert all(ending == ZERO for ending in cbd_only.values())
    nonzero = {key for key, ending in cbd.endings.items() if ending != ZERO}
    assert nonzero
    assert nonzero <= earlier


def test_customer_names_roundtrip_with_typo_preserved(loaded):
    conn, ids = loaded
    summary, detail, oi, cbd = _parse_all()
    expected = {row.customer_key for row in summary.summary_rows}
    expected |= {item.customer_key for item in detail.open_items}
    expected |= {item.customer_key for item in oi.open_items}
    expected |= set(cbd.endings)
    db = dict(conn.execute(
        "SELECT normalized_name, raw_name FROM customers WHERE tenant_id = %s",
        (TENANT,)).fetchall())
    assert set(db) == expected
    for row in summary.summary_rows:
        assert db[row.customer_key] == row.customer_raw
    assert any("Supression" in raw for raw in db.values())


# --- failure paths: the canonical layer must refuse and detect ---------------

def _summary_stub(total, rows, sha="stub-summary"):
    rep = reports.ParsedReport("aging_summary", "", "stub.csv", sha, "ascii")
    rep.grand_total = total
    for name, cur, d130, d3160, d6190, o90, tot in rows:
        rep.summary_rows.append(core.AgingSummaryRow(
            customer_raw=name, customer_key=core.normalize_customer_name(name),
            current=cur, d1_30=d130, d31_60=d3160, d61_90=d6190,
            over_90=o90, total=tot))
    return rep


def _detail_stub(items, grand, sha="stub-detail"):
    rep = reports.ParsedReport("aging_detail", "", "stub.csv", sha, "ascii")
    rep.grand_total = grand
    for name, num, bal, bucket, txn_type in items:
        rep.open_items.append(core.OpenItem(
            customer_raw=name, customer_key=core.normalize_customer_name(name),
            txn_type=txn_type, txn_date=date(2026, 5, 10), num=num, po_number="",
            terms="", due_date=date(2026, 6, 10), aging_days="", open_balance=bal,
            bucket=bucket, source_row_index=0))
    return rep


def _oi_stub(items, grand, sha="stub-oi"):
    rep = reports.ParsedReport("open_invoices", "", "stub.csv", sha, "ascii")
    rep.grand_total = grand
    for name, num, bal in items:
        rep.open_items.append(core.OpenItem(
            customer_raw=name, customer_key=core.normalize_customer_name(name),
            txn_type="Invoice", txn_date=date(2026, 5, 10), num=num, po_number="",
            terms="", due_date=date(2026, 6, 10), aging_days="", open_balance=bal,
            bucket=None, source_row_index=0))
    return rep


def _cbd_stub(endings, total, sha="stub-cbd"):
    rep = qcbd.CBDReport("stub.csv", sha, "ascii")
    rep.grand_total = total
    rep.endings = dict(endings)
    return rep


def _stub_batch(detail=None, summary=None):
    hundred = Decimal("100.00")
    if summary is None:
        summary = _summary_stub(hundred, [("Acme", ZERO, hundred, ZERO, ZERO, ZERO, hundred)])
    if detail is None:
        detail = _detail_stub([("Acme", "1", hundred, "1 - 30", "Invoice")], hundred)
    oi = _oi_stub([("Acme", "1", hundred)], hundred)
    cbd = _cbd_stub({"acme": hundred}, hundred)
    return summary, detail, oi, cbd


def test_tampered_transaction_amount_fails_gate(fresh_loaded):
    conn, ids = fresh_loaded
    conn.execute(
        "UPDATE ar_transactions SET amount = amount + 100.00 WHERE txn_id = ("
        "SELECT min(txn_id) FROM ar_transactions "
        "WHERE tenant_id = %s AND batch_id = %s AND direction = 'increase')",
        (TENANT, ids["aging_detail"]))
    gate = lc.run_canonical_gate(conn, TENANT, ids)
    assert gate.passed is False


def test_zero_amount_insert_raises_check_violation(fresh_loaded):
    conn, ids = fresh_loaded
    customer_id = conn.execute(
        "SELECT min(customer_id) FROM customers WHERE tenant_id = %s",
        (TENANT,)).fetchone()[0]
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO ar_transactions "
            "(tenant_id, batch_id, customer_id, invoice_number, txn_type, amount, direction) "
            "VALUES (%s, %s, %s, 'X', 'Invoice', 0, 'increase')",
            (TENANT, ids["aging_detail"], customer_id))


def test_unmapped_txn_type_refuses_load_and_rolls_back(empty_schema):
    conn = empty_schema
    detail = _detail_stub([("Acme", "1", Decimal("100.00"), "1 - 30", "Refund")],
                          Decimal("100.00"))
    summary, detail, oi, cbd = _stub_batch(detail=detail)
    with pytest.raises(lc.LoadError) as exc:
        lc.load_snapshot(conn, "stub", AS_OF, summary, detail, oi, cbd)
    assert exc.value.rule == "unmapped_txn_type"
    assert all(n == 0 for n in _counts(conn).values())


def test_quarantined_report_refuses_load(empty_schema):
    conn = empty_schema
    summary, detail, oi, cbd = _stub_batch()
    summary.quarantine.append(core.QuarantineItem(
        5, "summary_row_parse", "unparseable money value: 'GARBAGE'"))
    with pytest.raises(lc.LoadError) as exc:
        lc.load_snapshot(conn, "stub", AS_OF, summary, detail, oi, cbd)
    assert exc.value.rule == "quarantined_file"
    assert all(n == 0 for n in _counts(conn).values())


def test_decrease_type_with_positive_balance_refuses_load(empty_schema):
    conn = empty_schema
    detail = _detail_stub([("Acme", "1", Decimal("50.00"), "1 - 30", "Payment")],
                          Decimal("50.00"))
    summary, detail, oi, cbd = _stub_batch(detail=detail)
    with pytest.raises(lc.LoadError) as exc:
        lc.load_snapshot(conn, "stub", AS_OF, summary, detail, oi, cbd)
    assert exc.value.rule == "sign_violation"
    assert all(n == 0 for n in _counts(conn).values())


def test_tampered_reported_balance_makes_delta_nonzero(fresh_loaded):
    conn, ids = fresh_loaded
    assert conn.execute(
        "SELECT count(*) FROM invoices WHERE tenant_id = %s AND batch_id = %s AND delta <> 0",
        (TENANT, ids["aging_detail"])).fetchone()[0] == 0
    conn.execute(
        "UPDATE invoices SET reported_balance = reported_balance + 5.00 "
        "WHERE invoice_id = (SELECT min(invoice_id) FROM invoices "
        "WHERE tenant_id = %s AND batch_id = %s)",
        (TENANT, ids["aging_detail"]))
    rows = conn.execute(
        "SELECT delta FROM invoices WHERE tenant_id = %s AND batch_id = %s AND delta <> 0",
        (TENANT, ids["aging_detail"])).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == Decimal("-5")
