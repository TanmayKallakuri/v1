"""
qbd_reports.py - one parser per QBD report type.

Column positions below were verified directly against the real Industrial Pipe
& Supply exports (2026-05-31). QBD pads reports with blank separator columns,
so fields sit at fixed indices rather than being contiguous. Each parser tracks
the nested structure (bucket headers, customer headers, subtotal rows, grand
TOTAL) and unpivots to flat records, routing anything unparseable to quarantine
rather than failing the whole load mid-stream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from qbd_core import (
    AgingSummaryRow,
    OpenItem,
    QuarantineItem,
    detect_encoding,
    file_sha256,
    normalize_customer_name,
    parse_money,
    parse_qbd_date,
    read_rows,
)

# Transaction types observed in the real AR reports. General Journal is real
# and material: the entire ">90" bucket is composed of legacy General Journal
# entries that net to -13,341.66. Direction is how each type moves AR.
TXN_TYPE_DIRECTION = {
    "Invoice": "increase",
    "Payment": "decrease",
    "Discount": "decrease",
    "Credit Memo": "decrease",
    "General Journal": "signed",   # sign of the amount itself carries direction
}


@dataclass
class ParsedReport:
    report_type: str
    as_of_label: str
    source_path: str
    source_sha256: str
    encoding: str
    grand_total: Decimal | None = None
    open_items: list[OpenItem] = field(default_factory=list)
    summary_rows: list[AgingSummaryRow] = field(default_factory=list)
    bucket_totals: dict[str, Decimal] = field(default_factory=dict)
    customer_totals: dict[str, Decimal] = field(default_factory=dict)
    quarantine: list[QuarantineItem] = field(default_factory=list)


# ---------------------------------------------------------------------------
# A/R Aging Summary  (the control report; one row per customer, bucket columns)
# ---------------------------------------------------------------------------

def parse_aging_summary(path: str | Path) -> ParsedReport:
    path = Path(path)
    enc = detect_encoding(path)
    rows = read_rows(path, enc)
    rep = ParsedReport("aging_summary", "", str(path), file_sha256(path), enc)

    for idx, r in enumerate(rows):
        cells = [c.strip() for c in r]
        nonempty = [c for c in cells if c]
        if not nonempty:
            continue
        head = nonempty[0]
        if head in ("Current",) and "TOTAL" in nonempty:
            continue  # column header row
        if head == "TOTAL":
            try:
                rep.grand_total = parse_money(nonempty[-1])
            except ValueError as e:
                rep.quarantine.append(QuarantineItem(idx, "summary_grand_parse", str(e), r))
            continue
        # A data row is a customer name followed by the six bucket values. Take
        # everything after the name and require exactly six parseable money
        # cells. A malformed cell (e.g. "GARBAGE") must quarantine the row, not
        # be silently skipped, per the no-coercion rule.
        after_name = [c for c in cells[1:] if c != ""]
        if after_name:
            name = head
            if len(after_name) < 6:
                rep.quarantine.append(QuarantineItem(
                    idx, "summary_row_shape",
                    f"expected 6 money cells, found {len(after_name)}", r))
                continue
            try:
                m = [parse_money(v) for v in after_name[-6:]]
            except ValueError as e:
                rep.quarantine.append(QuarantineItem(idx, "summary_row_parse", str(e), r))
                continue
            m = [x if x is not None else Decimal("0") for x in m]
            rep.summary_rows.append(AgingSummaryRow(
                customer_raw=name, customer_key=normalize_customer_name(name),
                current=m[0], d1_30=m[1], d31_60=m[2], d61_90=m[3],
                over_90=m[4], total=m[5],
            ))
    return rep


# ---------------------------------------------------------------------------
# A/R Aging Detail  (bucket-organized; invoice + journal rows with due dates)
# ---------------------------------------------------------------------------

# Verified column indices for Aging Detail.
AD_C0 = 0       # bucket header / "Total <bucket>" lives in column 0
AD_TYPE = 3
AD_DATE = 5
AD_NUM = 7
AD_PO = 9
AD_NAME = 11
AD_TERMS = 13
AD_DUE = 15
AD_AGING = 17
AD_BAL = 19
AD_MIN = 20

_BUCKETS = ("Current", "1 - 30", "31 - 60", "61 - 90", "> 90")


def parse_aging_detail(path: str | Path) -> ParsedReport:
    path = Path(path)
    enc = detect_encoding(path)
    rows = read_rows(path, enc)
    rep = ParsedReport("aging_detail", "", str(path), file_sha256(path), enc)

    current_bucket: str | None = None
    for idx, r in enumerate(rows):
        cells = r + [""] * (AD_MIN - len(r)) if len(r) < AD_MIN else r
        c0 = cells[AD_C0].strip()
        typ = cells[AD_TYPE].strip()

        if typ == "Type":
            continue
        if c0 in _BUCKETS:
            current_bucket = c0
            continue
        if c0.startswith("Total "):
            label = c0[len("Total "):]
            try:
                amt = parse_money(cells[AD_BAL])
            except ValueError as e:
                rep.quarantine.append(QuarantineItem(idx, "detail_total_parse", str(e), r))
                continue
            if label in _BUCKETS:
                rep.bucket_totals[label] = amt or Decimal("0")
            continue
        if c0 == "TOTAL":
            try:
                rep.grand_total = parse_money(cells[AD_BAL])
            except ValueError as e:
                rep.quarantine.append(QuarantineItem(idx, "detail_grand_parse", str(e), r))
            continue
        if typ:  # an item row (Invoice or General Journal)
            try:
                bal = parse_money(cells[AD_BAL])
                d = parse_qbd_date(cells[AD_DATE])
                due = parse_qbd_date(cells[AD_DUE])
            except ValueError as e:
                rep.quarantine.append(QuarantineItem(idx, "detail_item_parse", str(e), r))
                continue
            if bal is None:
                rep.quarantine.append(QuarantineItem(idx, "detail_missing_balance",
                                                     "item row has no open balance", r))
                continue
            name = cells[AD_NAME].strip()
            rep.open_items.append(OpenItem(
                customer_raw=name, customer_key=normalize_customer_name(name),
                txn_type=typ, txn_date=d, num=cells[AD_NUM].strip(),
                po_number=cells[AD_PO].strip(), terms=cells[AD_TERMS].strip(),
                due_date=due, aging_days=cells[AD_AGING].strip(),
                open_balance=bal, bucket=current_bucket, source_row_index=idx,
            ))
    return rep


# ---------------------------------------------------------------------------
# Open Invoices  (customer-organized; due dates + terms, point-in-time open)
# ---------------------------------------------------------------------------

OI_NAME = 1
OI_TYPE = 4
OI_DATE = 6
OI_NUM = 8
OI_PO = 10
OI_TERMS = 12
OI_DUE = 14
OI_AGING = 16
OI_BAL = 18
OI_MIN = 19


def parse_open_invoices(path: str | Path) -> ParsedReport:
    path = Path(path)
    enc = detect_encoding(path)
    rows = read_rows(path, enc)
    rep = ParsedReport("open_invoices", "", str(path), file_sha256(path), enc)

    current_customer_raw: str | None = None
    current_customer_key: str | None = None
    for idx, r in enumerate(rows):
        cells = r + [""] * (OI_MIN - len(r)) if len(r) < OI_MIN else r
        name_col = cells[OI_NAME].strip()
        col0 = cells[0].strip()
        typ = cells[OI_TYPE].strip()

        if typ == "Type":
            continue
        if col0 == "TOTAL" or name_col == "TOTAL":
            try:
                rep.grand_total = parse_money(cells[OI_BAL])
            except ValueError as e:
                rep.quarantine.append(QuarantineItem(idx, "oi_grand_parse", str(e), r))
            continue
        if name_col.startswith("Total "):
            label = name_col[len("Total "):]
            try:
                amt = parse_money(cells[OI_BAL])
            except ValueError as e:
                rep.quarantine.append(QuarantineItem(idx, "oi_total_parse", str(e), r))
                continue
            rep.customer_totals[normalize_customer_name(label)] = amt or Decimal("0")
            continue
        if name_col and not typ:
            current_customer_raw = name_col
            current_customer_key = normalize_customer_name(name_col)
            continue
        if typ:  # item row under the current customer
            if current_customer_key is None:
                rep.quarantine.append(QuarantineItem(idx, "oi_orphan_item",
                                                     "item before any customer header", r))
                continue
            try:
                bal = parse_money(cells[OI_BAL])
                d = parse_qbd_date(cells[OI_DATE])
                due = parse_qbd_date(cells[OI_DUE])
            except ValueError as e:
                rep.quarantine.append(QuarantineItem(idx, "oi_item_parse", str(e), r))
                continue
            if bal is None:
                rep.quarantine.append(QuarantineItem(idx, "oi_missing_balance",
                                                     "item row has no open balance", r))
                continue
            rep.open_items.append(OpenItem(
                customer_raw=current_customer_raw or "", customer_key=current_customer_key,
                txn_type=typ, txn_date=d, num=cells[OI_NUM].strip(),
                po_number=cells[OI_PO].strip(), terms=cells[OI_TERMS].strip(),
                due_date=due, aging_days=cells[OI_AGING].strip(),
                open_balance=bal, bucket=None, source_row_index=idx,
            ))
    return rep


def unmapped_types(rep: ParsedReport) -> set[str]:
    """Transaction types present in the report that we have no direction for."""
    return {it.txn_type for it in rep.open_items if it.txn_type not in TXN_TYPE_DIRECTION}
