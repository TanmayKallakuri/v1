"""
Tests for the cash ingestion and reconciliation (parse level).

Run from this directory with the two real xlsx present in ./cash_files:
    python -m pytest test_cash.py -v

Covers the Balance Sheet parser, the GL cash extractor, and the cash gate on the
real files (the trust number is 451,068.87), plus synthetic failure paths that
prove the gate fails when GL and the Balance Sheet disagree. No database needed.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

import qbd_core as core
from qbd_balance_sheet import parse_balance_sheet
from qbd_gl_cash import parse_general_ledger
from cash_reconcile import reconcile_cash

DATA = Path("cash_files")
BS = DATA / "BS_May_31_2026.xlsx"
GL = DATA / "General_Ledger_April_May_2026.xlsx"

CASH_CONTROL = Decimal("451068.87")
BS_AR = Decimal("572191.85")          # GL/Balance Sheet AR account (11000)
AR_SUBLEDGER = Decimal("609772.89")   # AR Aging subledger total
AR_DIFFERENCE = Decimal("37581.04")   # known, expected aged-credit difference
AS_OF = date(2026, 5, 31)

real_only = pytest.mark.skipif(not BS.exists(), reason="real cash files not present in cash_files")


# --- cash account definition is config, in one place ------------------------

def test_cash_definition_is_the_three_banks():
    assert core.CASH_ACCOUNTS == frozenset({"10000", "10050", "10100"})
    assert core.CASH_PETTY_CASH == frozenset({"10200"})
    assert core.CASH_UNDEPOSITED == frozenset({"12000"})
    # Petty Cash and Undeposited Funds are deliberately not part of the total.
    assert "10200" not in core.CASH_ACCOUNTS
    assert "12000" not in core.CASH_ACCOUNTS


# --- Balance Sheet parser against the real file -----------------------------

@real_only
def test_bs_cash_control_total():
    bs = parse_balance_sheet(BS)
    assert bs.cash_control_total() == CASH_CONTROL
    assert len(bs.quarantine) == 0
    assert {a.number for a in bs.cash_accounts()} == {"10000", "10050", "10100"}


@real_only
def test_bs_as_of_date_parsed():
    bs = parse_balance_sheet(BS)
    assert bs.as_of_label == "May 31, 26"
    assert bs.as_of_date == AS_OF


@real_only
def test_bs_excluded_accounts_parsed_but_not_counted():
    bs = parse_balance_sheet(BS)
    petty = bs.petty()
    undep = bs.undeposited()
    assert len(petty) == 1 and petty[0].amount == Decimal("500")
    assert len(undep) == 1 and undep[0].amount == Decimal("101950.6")
    # they are parsed and reported, but never inside the cash control total
    excluded = {a.number for a in petty + undep}
    counted = {a.number for a in bs.cash_accounts()}
    assert excluded.isdisjoint(counted)


@real_only
def test_bs_ar_account_documents_the_gl_subledger_difference():
    bs = parse_balance_sheet(BS)
    ar = bs.ar_account()
    assert ar is not None
    assert ar.amount == BS_AR
    # the documented, expected difference vs the AR Aging subledger
    assert AR_SUBLEDGER - BS_AR == AR_DIFFERENCE


# --- GL cash extractor against the real file --------------------------------

@real_only
def test_gl_cash_total_ties():
    gl = parse_general_ledger(GL)
    assert gl.cash_total() == CASH_CONTROL
    assert set(gl.cash) == {"10000", "10050", "10100"}


@real_only
def test_gl_lands_full_raw_but_promotes_only_cash():
    gl = parse_general_ledger(GL)
    # the full GL is landed for audit; only the three cash accounts are promoted
    assert len(gl.all_accounts) > len(gl.cash)
    promoted = {a.number for a in gl.all_accounts if a.is_cash}
    assert promoted == {"10000", "10050", "10100"}
    assert len(gl.cash) == 3


@real_only
def test_gl_ending_is_opening_plus_transactions():
    gl = parse_general_ledger(GL)
    for a in gl.cash.values():
        assert a.ending == a.opening + sum((t.amount for t in a.txns), start=Decimal("0"))


# --- the cash gate on real data ---------------------------------------------

@real_only
def test_cash_gate_passes_on_real_files():
    bs = parse_balance_sheet(BS)
    gl = parse_general_ledger(GL)
    rr = reconcile_cash(gl.cash_by_account(), bs.cash_by_account())
    assert rr.passed is True
    assert len(rr.checks) == 3
    assert all(c.passed for c in rr.checks)
    assert rr.cash_total == CASH_CONTROL
    assert rr.cash_control == CASH_CONTROL


@real_only
def test_cash_gate_keystone_delta_is_zero():
    bs = parse_balance_sheet(BS)
    gl = parse_general_ledger(GL)
    rr = reconcile_cash(gl.cash_by_account(), bs.cash_by_account())
    keystone = next(c for c in rr.checks if "Keystone" in c.name)
    assert keystone.passed
    assert keystone.delta == Decimal("0.00")


@real_only
def test_cash_gate_per_account_ties():
    bs = parse_balance_sheet(BS)
    gl = parse_general_ledger(GL)
    gl_by = gl.cash_by_account()
    bs_by = bs.cash_by_account()
    for num in gl_by:
        assert gl_by[num] == bs_by[num]


# --- failure paths: prove the cash gate FAILS when it should -----------------

def test_cash_gate_fails_when_totals_disagree():
    gl_by = {"10000": Decimal("100.00"), "10050": Decimal("200.00")}
    bs_by = {"10000": Decimal("100.00"), "10050": Decimal("250.00")}  # 50 off
    rr = reconcile_cash(gl_by, bs_by)
    assert rr.passed is False
    assert any("Keystone" in c.name and not c.passed for c in rr.checks)
    assert any("Per-account" in c.name and not c.passed for c in rr.checks)


def test_cash_gate_fails_when_account_missing():
    # totals tie in aggregate but a bank account is present on only one side
    gl_by = {"10000": Decimal("300.00")}
    bs_by = {"10000": Decimal("100.00"), "10050": Decimal("200.00")}
    rr = reconcile_cash(gl_by, bs_by)
    assert rr.passed is False
    assert any("coverage" in c.name.lower() and not c.passed for c in rr.checks)


def test_cash_gate_passes_on_synthetic_tie():
    gl_by = {"10000": Decimal("1.00"), "10050": Decimal("2.00")}
    bs_by = {"10000": Decimal("1.00"), "10050": Decimal("2.00")}
    rr = reconcile_cash(gl_by, bs_by)
    assert rr.passed is True
