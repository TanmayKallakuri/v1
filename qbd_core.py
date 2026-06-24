"""
qbd_core.py - shared primitives for parsing QuickBooks Desktop CSV exports.

Everything in this module is report-type agnostic: encoding detection, money
and date parsing, customer-name normalization, file hashing, and the small
dataclasses the report parsers and the reconciliation suite share.

All money is Decimal. No floats touch a monetary value anywhere in the pipeline.
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

# Absolute reconciliation tolerance, per the architecture contract and the
# workflow doc Stage 4: every tie-out check uses one cent of slack.
RECON_TOLERANCE = Decimal("0.01")

# --- Cash account definition (assumption #1) --------------------------------
# Which general ledger accounts make up "Cash" for the Cash tile. This is the
# single source of truth: the Balance Sheet parser, the GL cash extractor, and
# the cash reconciliation gate all import these constants, so widening or
# narrowing the cash definition is a one-line change here, never a hunt through
# modules. See ASSUMPTIONS.md item 1.
#
# v1 decision: the three spendable bank accounts only. Petty Cash and
# Undeposited Funds are parsed and reported but deliberately excluded from the
# cash total (Petty Cash is immaterial; Undeposited Funds double-counts against
# the bank accounts and is not cash in hand). To count either as cash, add it
# to CASH_ACCOUNTS once the client confirms; nothing else changes.
CASH_BANK_ACCOUNTS = frozenset({"10000", "10050", "10100"})
CASH_PETTY_CASH = frozenset({"10200"})
CASH_UNDEPOSITED = frozenset({"12000"})
CASH_ACCOUNTS = CASH_BANK_ACCOUNTS

# Encodings observed across the real batch, tried in order. Three of the four
# files are plain ASCII; Customer Balance Detail is cp1252. ASCII is a strict
# subset of utf-8, and utf-8 failures fall through to cp1252, which decodes any
# byte. Detection is per file, never assumed for the batch.
_ENCODINGS = ("ascii", "utf-8", "cp1252")


def detect_encoding(path: Path) -> str:
    """Return the first encoding from _ENCODINGS that decodes the file cleanly."""
    raw = path.read_bytes()
    for enc in _ENCODINGS:
        try:
            raw.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    # cp1252 maps every byte, so this is effectively unreachable, but be explicit.
    raise ValueError(f"could not decode {path.name} with any known encoding")


def file_sha256(path: Path) -> str:
    """SHA-256 of the raw bytes, for the immutable-landing provenance record."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_money(value: str) -> Decimal | None:
    """Parse a QBD money string to Decimal.

    Handles thousands separators ("92,306.89"), leading-minus negatives
    ("-13,341.66"), and parenthesized negatives "(1,234.00)". A blank cell
    returns None so callers can tell "absent" from "zero". Never returns float.
    """
    s = value.strip().replace(",", "")
    if s == "":
        return None
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    try:
        return Decimal(s)
    except InvalidOperation as exc:
        raise ValueError(f"unparseable money value: {value!r}") from exc


def parse_qbd_date(value: str) -> date | None:
    """Parse a QBD MM/DD/YYYY date string. Blank returns None."""
    s = value.strip()
    if s == "":
        return None
    try:
        return datetime.strptime(s, "%m/%d/%Y").date()
    except ValueError as exc:
        raise ValueError(f"unparseable date value: {value!r}") from exc


def normalize_customer_name(raw: str) -> str:
    """Canonical join key for a customer name.

    Names are the only key QBD exports and they are dirty: trailing whitespace,
    internal double spaces ("Alpha Fire  - Chicago Fire"), trailing dashes
    ("Nicor Gas-"). Collapse internal whitespace, strip ends, casefold. Note
    this intentionally does NOT fix source-side typos like "Supression"; those
    are surfaced for human review, not silently rewritten.
    """
    import re
    return re.sub(r"\s+", " ", raw).strip().casefold()


def read_rows(path: Path, encoding: str) -> list[list[str]]:
    """Read a CSV into a list of cell-lists using the detected encoding."""
    with path.open(encoding=encoding, newline="") as fh:
        return list(csv.reader(fh))


# --- shared records ---------------------------------------------------------

@dataclass(frozen=True)
class OpenItem:
    """One open AR item (invoice or journal entry) from a detail-level report."""
    customer_raw: str
    customer_key: str
    txn_type: str
    txn_date: date | None
    num: str
    po_number: str
    terms: str
    due_date: date | None
    aging_days: str
    open_balance: Decimal
    bucket: str | None          # set by Aging Detail (which bucket it sits in)
    source_row_index: int


@dataclass(frozen=True)
class AgingSummaryRow:
    """One customer row from the A/R Aging Summary (the control report)."""
    customer_raw: str
    customer_key: str
    current: Decimal
    d1_30: Decimal
    d31_60: Decimal
    d61_90: Decimal
    over_90: Decimal
    total: Decimal


@dataclass
class QuarantineItem:
    row_index: int
    rule: str
    reason: str
    raw: list[str] = field(default_factory=list)
