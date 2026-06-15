"""
Tests for the full QBD multi-report ingestion and reconciliation suite.

Run from this directory with the four real CSVs present in ./ips_files:
    python3 -m pytest test_batch.py -v

Covers: core parsing primitives, each report parser against the real files,
the six-check reconciliation gate (happy path on real data), and synthetic
failure paths that prove the gate fails when it should.
"""

from decimal import Decimal
from pathlib import Path

import pytest

import qbd_core as core
import qbd_reports as reports
import qbd_cbd as cbd
import qbd_reconcile as recon

DATA = Path("ips_files")
SUMMARY = DATA / "IndustrialPipe_AR-Aging-Summary_2026-05-31.csv"
DETAIL = DATA / "IndustrialPipe_AR-Aging-Detail_2026-05-31.csv"
OPENINV = DATA / "IndustrialPipe_OpenInvoices _2026-05-31.csv"
CBD = DATA / "IndustrialPipe_CustomerBalanceDetail_2026_05.csv"

AR_TOTAL = Decimal("609772.89")
OPENINV_TOTAL = Decimal("524144.48")
GAP = Decimal("85628.41")

real_only = pytest.mark.skipif(not SUMMARY.exists(), reason="real files not present")


# --- core primitives --------------------------------------------------------

def test_money_parsing():
    assert core.parse_money("92,306.89") == Decimal("92306.89")
    assert core.parse_money("-13,341.66") == Decimal("-13341.66")
    assert core.parse_money("(1,234.00)") == Decimal("-1234.00")
    assert core.parse_money("") is None
    assert isinstance(core.parse_money("5,500.00"), Decimal)


def test_money_rejects_garbage():
    with pytest.raises(ValueError):
        core.parse_money("twelve")


def test_name_normalization():
    assert core.normalize_customer_name("Alpha Fire  - Chicago Fire") == "alpha fire - chicago fire"
    assert core.normalize_customer_name("Nicor Gas- ") == "nicor gas-"


def test_date_parsing():
    d = core.parse_qbd_date("05/31/2026")
    assert (d.year, d.month, d.day) == (2026, 5, 31)
    assert core.parse_qbd_date("") is None


@real_only
def test_encoding_detection_matches_reality():
    assert core.detect_encoding(SUMMARY) == "ascii"
    assert core.detect_encoding(CBD) == "cp1252"


# --- parsers against the real files ----------------------------------------

@real_only
def test_aging_summary_control_total():
    rep = reports.parse_aging_summary(SUMMARY)
    assert rep.grand_total == AR_TOTAL
    assert len(rep.quarantine) == 0


@real_only
def test_aging_detail_total_and_general_journal_present():
    rep = reports.parse_aging_detail(DETAIL)
    assert rep.grand_total == AR_TOTAL
    types = {it.txn_type for it in rep.open_items}
    assert "General Journal" in types          # the legacy entries are real
    assert "Invoice" in types
    assert reports.unmapped_types(rep) == set()  # all types are direction-mapped
    assert len(rep.quarantine) == 0


@real_only
def test_aging_detail_bucket_totals():
    rep = reports.parse_aging_detail(DETAIL)
    assert rep.bucket_totals["Current"] == Decimal("86755.84")
    assert rep.bucket_totals["> 90"] == Decimal("-13341.66")


@real_only
def test_open_invoices_total_and_gap():
    rep = reports.parse_open_invoices(OPENINV)
    assert rep.grand_total == OPENINV_TOTAL
    assert AR_TOTAL - OPENINV_TOTAL == GAP


@real_only
def test_cbd_endings_sum_to_control():
    rep = cbd.parse_customer_balance_detail(CBD)
    assert cbd.grand_total(rep) == AR_TOTAL


# --- the six-check gate on real data ---------------------------------------

@real_only
def test_full_gate_passes_on_real_batch():
    summary = reports.parse_aging_summary(SUMMARY)
    detail = reports.parse_aging_detail(DETAIL)
    oi = reports.parse_open_invoices(OPENINV)
    c = cbd.parse_customer_balance_detail(CBD)
    rr = recon.reconcile_batch(summary, detail, oi,
                               cbd_customer_endings=cbd.customer_endings(c),
                               cbd_grand_total=cbd.grand_total(c))
    assert rr.passed is True
    assert len(rr.checks) == 6
    assert all(c.passed for c in rr.checks)


@real_only
def test_open_invoices_quirk_resolves_to_zero_residual():
    summary = reports.parse_aging_summary(SUMMARY)
    detail = reports.parse_aging_detail(DETAIL)
    oi = reports.parse_open_invoices(OPENINV)
    rr = recon.reconcile_batch(summary, detail, oi)
    quirk = next(c for c in rr.checks if "Open Invoices" in c.name)
    assert quirk.passed
    assert quirk.delta == Decimal("0.00")


# --- failure paths: prove the gate FAILS when it should ---------------------

def _summary_stub(total, rows):
    """Build a minimal ParsedReport standing in for an Aging Summary."""
    rep = reports.ParsedReport("aging_summary", "", "stub", "x", "ascii")
    rep.grand_total = total
    for name, cur, d130, d3160, d6190, o90, tot in rows:
        rep.summary_rows.append(core.AgingSummaryRow(
            customer_raw=name, customer_key=core.normalize_customer_name(name),
            current=cur, d1_30=d130, d31_60=d3160, d61_90=d6190,
            over_90=o90, total=tot))
    return rep


def _detail_stub(items, grand, buckets=None):
    rep = reports.ParsedReport("aging_detail", "", "stub", "x", "ascii")
    rep.grand_total = grand
    rep.bucket_totals = buckets or {}
    for (name, num, bal, bucket) in items:
        rep.open_items.append(core.OpenItem(
            customer_raw=name, customer_key=core.normalize_customer_name(name),
            txn_type="Invoice", txn_date=None, num=num, po_number="",
            terms="", due_date=None, aging_days="", open_balance=bal,
            bucket=bucket, source_row_index=0))
    return rep


def _oi_stub(items, grand):
    rep = reports.ParsedReport("open_invoices", "", "stub", "x", "ascii")
    rep.grand_total = grand
    for (name, num, bal) in items:
        rep.open_items.append(core.OpenItem(
            customer_raw=name, customer_key=core.normalize_customer_name(name),
            txn_type="Invoice", txn_date=None, num=num, po_number="",
            terms="", due_date=None, aging_days="", open_balance=bal,
            bucket=None, source_row_index=0))
    return rep


def test_gate_fails_when_detail_disagrees_with_control():
    # Aging Detail sums to 100 but the control says 200: keystone must FAIL.
    summary = _summary_stub(Decimal("200.00"),
                            [("Acme", Decimal("0"), Decimal("200"), Decimal("0"),
                              Decimal("0"), Decimal("0"), Decimal("200"))])
    detail = _detail_stub([("Acme", "1", Decimal("100.00"), "1 - 30")],
                          grand=Decimal("100.00"))
    oi = _oi_stub([("Acme", "1", Decimal("100.00"))], grand=Decimal("100.00"))
    rr = recon.reconcile_batch(summary, detail, oi)
    assert rr.passed is False
    assert any("Keystone" in c.name and not c.passed for c in rr.checks)


def test_gate_fails_on_per_customer_mismatch():
    # Totals tie in aggregate but a customer row disagrees.
    summary = _summary_stub(Decimal("100.00"),
                            [("Acme", Decimal("0"), Decimal("60"), Decimal("0"),
                              Decimal("0"), Decimal("0"), Decimal("60.00")),
                             ("Beta", Decimal("0"), Decimal("40"), Decimal("0"),
                              Decimal("0"), Decimal("0"), Decimal("40.00"))])
    # Detail puts 100 all under Acme: aggregate ties, per-customer does not.
    detail = _detail_stub([("Acme", "1", Decimal("100.00"), "1 - 30")],
                          grand=Decimal("100.00"))
    oi = _oi_stub([("Acme", "1", Decimal("100.00"))], grand=Decimal("100.00"))
    rr = recon.reconcile_batch(summary, detail, oi)
    assert rr.passed is False
    assert any("Per-customer" in c.name and not c.passed for c in rr.checks)


def test_gate_fails_when_open_invoices_gap_unexplained():
    # Open Invoices is short by an invoice that is NOT in Aging Detail:
    # the shortfall is unexplained, so the quirk check must FAIL.
    summary = _summary_stub(Decimal("100.00"),
                            [("Acme", Decimal("0"), Decimal("100"), Decimal("0"),
                              Decimal("0"), Decimal("0"), Decimal("100.00"))])
    detail = _detail_stub([("Acme", "1", Decimal("100.00"), "1 - 30")],
                          grand=Decimal("100.00"))
    # OI claims only 40 open and references an invoice "9" unknown to Aging.
    oi = _oi_stub([("Acme", "9", Decimal("40.00"))], grand=Decimal("40.00"))
    rr = recon.reconcile_batch(summary, detail, oi)
    quirk = next(c for c in rr.checks if "Open Invoices" in c.name)
    assert quirk.passed is False


def test_garbage_money_quarantines_file_not_crashes(tmp_path):
    bad = tmp_path / "IndustrialPipe_AR-Aging-Summary_2026-05-31.csv"
    bad.write_text(
        "Current,1 - 30,31 - 60,61 - 90,> 90,TOTAL\n"
        "Acme,0.00,GARBAGE,0.00,0.00,0.00,100.00\n"
        "TOTAL,0.00,0.00,0.00,0.00,0.00,100.00\n",
        encoding="ascii")
    rep = reports.parse_aging_summary(bad)
    assert len(rep.quarantine) == 1
    assert rep.quarantine[0].rule == "summary_row_parse"
