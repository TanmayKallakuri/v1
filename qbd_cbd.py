"""
qbd_cbd.py - Customer Balance Detail parser, on the shared core.

Reuses the structure from the first-session parser: nested per-customer ledger
with running balances, a "Total <customer>" subtotal per customer, and a
flush-left grand TOTAL row (column 0, unlike the indented customer rows).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from qbd_core import (
    QuarantineItem,
    detect_encoding,
    file_sha256,
    normalize_customer_name,
    parse_money,
    read_rows,
)

CBD_NAME = 1
CBD_TYPE = 4
CBD_BAL = 14
CBD_MIN = 15


@dataclass
class CBDReport:
    source_path: str
    source_sha256: str
    encoding: str
    grand_total: Decimal | None = None
    # customer_key -> reported ending balance (from the "Total <name>" row)
    endings: dict[str, Decimal] = field(default_factory=dict)
    quarantine: list[QuarantineItem] = field(default_factory=list)


def parse_customer_balance_detail(path: str | Path) -> CBDReport:
    path = Path(path)
    enc = detect_encoding(path)
    rows = read_rows(path, enc)
    rep = CBDReport(str(path), file_sha256(path), enc)

    for idx, r in enumerate(rows):
        cells = r + [""] * (CBD_MIN - len(r)) if len(r) < CBD_MIN else r
        col0 = cells[0].strip()
        name = cells[CBD_NAME].strip()
        typ = cells[CBD_TYPE].strip()

        if name == "" and typ == "Type":
            continue
        if col0 == "TOTAL" or name == "TOTAL":
            try:
                rep.grand_total = parse_money(cells[CBD_BAL])
            except ValueError as e:
                rep.quarantine.append(QuarantineItem(idx, "cbd_grand_parse", str(e), r))
            continue
        if name.startswith("Total "):
            label = name[len("Total "):]
            try:
                bal = parse_money(cells[CBD_BAL])
            except ValueError as e:
                rep.quarantine.append(QuarantineItem(idx, "cbd_subtotal_parse", str(e), r))
                continue
            rep.endings[normalize_customer_name(label)] = bal or Decimal("0")

    return rep


def customer_endings(rep: CBDReport) -> dict[str, Decimal]:
    return rep.endings


def grand_total(rep: CBDReport) -> Decimal | None:
    # If the file had no flush-left TOTAL, fall back to summing customer endings.
    if rep.grand_total is not None:
        return rep.grand_total
    if rep.endings:
        return sum(rep.endings.values(), start=Decimal("0"))
    return None
