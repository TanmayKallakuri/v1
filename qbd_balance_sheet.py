"""
qbd_balance_sheet.py - parser for the QBD Balance Sheet Standard export (.xlsx).

Mirrors the AR report parsers (qbd_reports.py): one parser that tracks the
nested structure and unpivots to flat account records, with file-level
provenance (sha256, encoding label) for the immutable-landing record.

v1 goal: extract the as-of date, the per-account cash balances, and the cash
control total, so the Cash tile can reconcile the GL-computed cash total against
this Balance Sheet (the trust number is 451,068.87).

Structure learned from the real IPS file (BS_May_31_2026.xlsx), reused verbatim
from the proven prototype, not re-derived:
- as-of date sits in the first row, first non-empty cell.
- indentation is encoded by COLUMN position: deeper column = deeper nesting.
- account rows: a cell like "10000 - Operating Acc..." plus a numeric amount in
  column 5.
- subtotal rows ("Total Checking/Savings") carry a formula string, not a number,
  so they are skipped rather than parsed.
- the cash accounts live under the "Checking/Savings" group; Undeposited Funds
  (12000) lives separately under "Other Current Assets".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import openpyxl

from qbd_core import (
    CASH_ACCOUNTS,
    CASH_PETTY_CASH,
    CASH_UNDEPOSITED,
    QuarantineItem,
    file_sha256,
)

# Account number plus name, separated by a dash or the QBD middle dot. Reused
# from the prototype.
ACCT_RE = re.compile(r"^\s*(\d{4,5})\s*[·\-]\s*(.+?)\s*$")

# The single account that carries AR on the Balance Sheet. Surfaced so the cash
# work can also document the GL/Balance-Sheet AR figure (572,191.85) against the
# AR Aging subledger total (609,772.89). See ASSUMPTIONS.md.
AR_ACCOUNT = "11000"

# Column holding the leaf amount, learned from the real file.
BS_AMOUNT_COL = 5

# Accepted as-of date formats. The real file prints "May 31, 26".
_ASOF_FORMATS = ("%b %d, %y", "%B %d, %y", "%b %d, %Y", "%B %d, %Y")


@dataclass(frozen=True)
class BSAccount:
    number: str
    name: str
    amount: Decimal
    group: str          # the section it sits under, e.g. "Checking/Savings"


@dataclass
class BalanceSheet:
    report_type: str
    as_of_label: str
    as_of_date: date | None
    source_path: str
    source_sha256: str
    encoding: str
    accounts: list[BSAccount] = field(default_factory=list)   # all leaf accounts with amounts
    quarantine: list[QuarantineItem] = field(default_factory=list)

    def cash_accounts(self) -> list[BSAccount]:
        return [a for a in self.accounts if a.number in CASH_ACCOUNTS]

    def petty(self) -> list[BSAccount]:
        return [a for a in self.accounts if a.number in CASH_PETTY_CASH]

    def undeposited(self) -> list[BSAccount]:
        return [a for a in self.accounts if a.number in CASH_UNDEPOSITED]

    def ar_account(self) -> BSAccount | None:
        return next((a for a in self.accounts if a.number == AR_ACCOUNT), None)

    def cash_control_total(self) -> Decimal:
        """The cash control total: the sum of the in-scope cash accounts only."""
        return sum((a.amount for a in self.cash_accounts()), start=Decimal("0"))

    def cash_by_account(self) -> dict[str, Decimal]:
        return {a.number: a.amount for a in self.cash_accounts()}


def _cell_money(value, where: str) -> Decimal | None:
    """Parse a Balance Sheet amount cell to Decimal, never float.

    Numeric cells come through as int/float; convert via str so no binary float
    ever touches a monetary value. String cells may be a real number or a
    formula (starting with "="); formulas and blanks are not leaf amounts and
    return None. Anything else is a hard parse error for the caller to quarantine.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str) and not value.startswith("="):
        s = value.replace(",", "").replace("$", "").strip()
        if s == "":
            return None
        try:
            return Decimal(s)
        except InvalidOperation as exc:
            raise ValueError(f"unparseable money at {where}: {value!r}") from exc
    return None  # formula or empty -> not a leaf amount


def _parse_asof(label: str | None) -> date | None:
    if not label:
        return None
    for fmt in _ASOF_FORMATS:
        try:
            return datetime.strptime(label.strip(), fmt).date()
        except ValueError:
            continue
    return None


def parse_balance_sheet(path: str | Path) -> BalanceSheet:
    path = Path(path)
    sha = file_sha256(path)
    # The .xlsx is a binary OOXML container, not a text encoding we sniff like
    # the CSV reports; record a fixed label for the provenance row.
    rep = BalanceSheet("balance_sheet", "", None, str(path), sha, "xlsx")

    wb = openpyxl.load_workbook(path, read_only=True, data_only=False)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    # as-of date: first non-empty cell in the header row.
    as_of = None
    if rows:
        for v in rows[0]:
            if v is not None:
                as_of = str(v)
                break
    rep.as_of_label = as_of or ""
    rep.as_of_date = _parse_asof(as_of)

    current_group: str | None = None
    for ri, r in enumerate(rows):
        if ri == 0:
            continue  # header row holds the as-of date, not data
        cells = [(j, v) for j, v in enumerate(r) if v is not None]
        if not cells:
            continue
        # label is the first textual cell; amount (if any) is the numeric in the
        # learned amount column.
        _, label = cells[0]
        amount = None
        for j, v in cells:
            if j == BS_AMOUNT_COL:
                try:
                    amount = _cell_money(v, f"row {ri} {label!r}")
                except ValueError as exc:
                    rep.quarantine.append(QuarantineItem(ri, "bs_amount_parse", str(exc), [str(c) for c in r]))
                    amount = None
                break
        m = ACCT_RE.match(str(label))
        if m and amount is not None:
            # a leaf account row with a real number
            num, name = m.group(1), m.group(2).strip()
            rep.accounts.append(BSAccount(num, name, amount, current_group or "?"))
        elif not m:
            # a section/group label (no account number). Track the nearest group.
            txt = str(label).strip()
            if not txt.startswith("Total") and amount is None:
                current_group = txt
    return rep
