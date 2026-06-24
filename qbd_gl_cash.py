"""
qbd_gl_cash.py - cash extractor for the QBD General Ledger export (.xlsx).

Mirrors the AR report parsers (qbd_reports.py): file-level provenance plus flat
records, here scoped to cash. v1 deliberately pulls ONLY the cash accounts from
the full GL and computes each one's ending balance (opening + period
transactions); the cash total must tie to the Balance Sheet cash control
(451,068.87). The full GL carries every account the business runs (82 in this
batch, 164 in the prototype's reference file); everything that is not cash is
landed as a raw account header for audit but never promoted to the cash tables.

Structure learned from the real IPS file (General_Ledger_April_May_2026.xlsx),
reused verbatim from the proven prototype, not re-derived:
- account header row: column 1 holds "10000 - Operating Acc...", column 21 holds
  the account OPENING balance for the period.
- transaction rows: column 7 = type, column 9 = date, column 13 = name, column
  17 = split, column 19 = amount (signed), column 21 = a running-balance FORMULA.
- "Total <account>" row closes the block.
Because the running-balance column is formulas (which openpyxl will not
evaluate), the ending balance is computed as opening + sum(transaction amounts).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

import openpyxl

from qbd_core import CASH_ACCOUNTS, file_sha256

ACCT_RE = re.compile(r"^\s*(\d{4,5})\s*[·\-]\s*(.+?)\s*$")

# Column indices (0-based) learned from the real file. Reused from the prototype.
C_ACCT = 1
C_TYPE = 7
C_DATE = 9
C_NUM = 11
C_NAME = 13
C_SPLIT = 17
C_AMT = 19
C_BAL = 21


@dataclass(frozen=True)
class CashTxn:
    account: str
    txn_type: str
    date: object
    name: str
    amount: Decimal


@dataclass
class CashAccount:
    number: str
    name: str
    opening: Decimal
    ending: Decimal
    txns: list[CashTxn] = field(default_factory=list)


@dataclass(frozen=True)
class GLAccount:
    """A raw GL account header: number, name, opening balance, cash flag.

    Landed for audit for every account in the file, cash or not, so the full GL
    is traceable without promoting 164 accounts of transactions.
    """
    number: str
    name: str
    opening: Decimal
    is_cash: bool


@dataclass
class GLCashReport:
    report_type: str
    as_of_label: str
    as_of_date: date | None
    source_path: str
    source_sha256: str
    encoding: str
    cash: dict[str, CashAccount] = field(default_factory=dict)        # promoted: cash accounts only
    all_accounts: list[GLAccount] = field(default_factory=list)       # raw landing: every account header

    def cash_total(self) -> Decimal:
        return sum((a.ending for a in self.cash.values()), start=Decimal("0"))

    def cash_by_account(self) -> dict[str, Decimal]:
        return {num: a.ending for num, a in self.cash.items()}


def _dec(v) -> Decimal | None:
    if isinstance(v, bool):
        return None
    return Decimal(str(v)) if isinstance(v, (int, float)) else None


def parse_general_ledger(path: str | Path, accounts_wanted=CASH_ACCOUNTS) -> GLCashReport:
    path = Path(path)
    sha = file_sha256(path)
    rep = GLCashReport("general_ledger", "", None, str(path), sha, "xlsx")

    wb = openpyxl.load_workbook(path, read_only=True, data_only=False)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    cur: CashAccount | None = None  # current cash account being filled, if wanted
    cur_num: str | None = None
    for r in rows:
        acct = r[C_ACCT] if len(r) > C_ACCT else None
        typ = r[C_TYPE] if len(r) > C_TYPE else None
        amt = r[C_AMT] if len(r) > C_AMT else None
        bal = r[C_BAL] if len(r) > C_BAL else None

        # account header row
        if acct and isinstance(acct, str) and ("·" in acct or "-" in acct):
            m = ACCT_RE.match(acct)
            if m and not acct.strip().startswith("Total"):
                num, name = m.group(1), m.group(2).strip()
                opening = _dec(bal) or Decimal("0")
                is_cash = num in accounts_wanted
                # land every account header for audit, cash or not
                rep.all_accounts.append(GLAccount(num, name, opening, is_cash))
                if is_cash:
                    cur_num = num
                    cur = CashAccount(num, name, opening, opening)
                    rep.cash[num] = cur
                else:
                    cur = None
                    cur_num = None
                continue

        # total row closes the block
        if acct and isinstance(acct, str) and acct.strip().startswith("Total"):
            cur = None
            cur_num = None
            continue

        # transaction row inside a wanted (cash) account
        if cur is not None and typ and isinstance(amt, (int, float)) and not isinstance(amt, bool):
            a = Decimal(str(amt))
            cur.txns.append(CashTxn(
                cur_num, typ,
                r[C_DATE] if len(r) > C_DATE else None,
                r[C_NAME] if len(r) > C_NAME else None,
                a))
            cur.ending += a
    return rep
