"""
load_canonical.py - Stage 3: canonical Postgres load and database-side gate.

Loads the four parsed QBD reports into the canonical schema (schema.sql), one
batch per file, all four in a single transaction. Then proves the database can
stand on its own: the gate inputs are reconstructed purely from Postgres rows
and fed to qbd_reconcile.reconcile_batch unmodified.

Usage:
    python load_canonical.py [directory] --tenant ips --as-of 2026-05-31

Requires DATABASE_URL, e.g. postgresql://qbd:qbd@localhost:5433/qbd
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg

from qbd_core import AgingSummaryRow, OpenItem
from qbd_cbd import CBDReport, parse_customer_balance_detail
from qbd_reports import (
    ParsedReport,
    TXN_TYPE_DIRECTION,
    parse_aging_detail,
    parse_aging_summary,
    parse_open_invoices,
)
from qbd_reconcile import ReconReport, reconcile_batch

CENT = Decimal("0.01")
BUCKETS = ("Current", "1 - 30", "31 - 60", "61 - 90", "> 90")
SCHEMA_PATH = Path(__file__).with_name("schema.sql")

SIGNED_SUM_SQL = (
    "SELECT COALESCE(SUM(CASE WHEN direction = 'increase' THEN amount ELSE -amount END), 0) "
    "FROM ar_transactions WHERE tenant_id = %s AND batch_id = %s"
)


class LoadError(Exception):
    """A validation failure that refuses the whole file, machine readable."""

    def __init__(self, rule: str, reason: str):
        self.rule = rule
        self.reason = reason
        super().__init__(f"{rule}: {reason}")


def get_conn() -> psycopg.Connection:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise LoadError(
            "missing_database_url",
            "set the DATABASE_URL environment variable to a Postgres connection string",
        )
    return psycopg.connect(url, autocommit=True)


def apply_schema(conn: psycopg.Connection) -> None:
    with conn.transaction():
        conn.execute(SCHEMA_PATH.read_text(encoding="ascii"))


def as_money(value: Decimal, context: str) -> Decimal:
    quantized = value.quantize(CENT)
    if quantized != value:
        raise LoadError("money_precision", f"{context}: value {value} is not exact to the cent")
    return quantized


def map_sign(txn_type: str, open_balance: Decimal) -> tuple[Decimal, str]:
    """Positive magnitude + direction for the ledger, per TXN_TYPE_DIRECTION."""
    mapping = TXN_TYPE_DIRECTION.get(txn_type)
    if mapping is None:
        raise LoadError("unmapped_txn_type", f"no direction mapping for transaction type {txn_type!r}")
    if mapping == "increase":
        if open_balance <= 0:
            raise LoadError(
                "sign_violation",
                f"{txn_type} item must carry a positive open balance, got {open_balance}",
            )
        return open_balance, "increase"
    if mapping == "decrease":
        if open_balance >= 0:
            raise LoadError(
                "sign_violation",
                f"{txn_type} item must carry a negative open balance, got {open_balance}",
            )
        return -open_balance, "decrease"
    if open_balance == 0:
        raise LoadError(
            "sign_violation",
            f"{txn_type} item has a zero open balance, direction is undecidable",
        )
    if open_balance > 0:
        return open_balance, "increase"
    return -open_balance, "decrease"


def derive_bucket(as_of_date: date, txn_date: date | None, due_date: date | None) -> str:
    """Recompute an aging bucket from dates. NOT the source of truth.

    The authoritative bucket is qbd_bucket, the value QuickBooks reported and we
    stored. This function is retained for a planned v1.1 soft-validation check:
    recompute the bucket from dates and flag (do not fail) any disagreement with
    qbd_bucket, which can surface a QuickBooks-side aging anomaly. It is
    deliberately NOT wired into the readback path, because letting a recomputed
    value stand in for QuickBooks' own number is exactly the fragility this
    change removed. QBD appears to age from the transaction date, falling back to
    the due date when the transaction date is absent, but that heuristic is only
    as trustworthy as one month of one tenant's data, which is why it validates
    rather than decides.
    """
    effective = txn_date if txn_date is not None else due_date
    if effective is None:
        raise LoadError("missing_dates", "item has neither a transaction date nor a due date")
    days = (as_of_date - effective).days
    if days <= 0:
        return "Current"
    if days <= 30:
        return "1 - 30"
    if days <= 60:
        return "31 - 60"
    if days <= 90:
        return "61 - 90"
    return "> 90"


def upsert_customer(conn, tenant_id: str, normalized_name: str, raw_name: str,
                    terms: str | None = None) -> int:
    row = conn.execute(
        "INSERT INTO customers (tenant_id, normalized_name, raw_name, terms) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (tenant_id, normalized_name) DO NOTHING "
        "RETURNING customer_id",
        (tenant_id, normalized_name, raw_name, terms),
    ).fetchone()
    if row is not None:
        return row[0]
    return conn.execute(
        "SELECT customer_id FROM customers WHERE tenant_id = %s AND normalized_name = %s",
        (tenant_id, normalized_name),
    ).fetchone()[0]


def _require_clean(label: str, quarantine: list) -> None:
    if quarantine:
        first = quarantine[0]
        raise LoadError(
            "quarantined_file",
            f"{label} has {len(quarantine)} quarantined rows; "
            f"first is row {first.row_index} rule {first.rule}: {first.reason}",
        )


def _existing_batch(conn, tenant_id: str, sha256: str) -> uuid.UUID | None:
    row = conn.execute(
        "SELECT batch_id FROM batches WHERE tenant_id = %s AND sha256 = %s",
        (tenant_id, sha256),
    ).fetchone()
    return row[0] if row else None


def _insert_batch(conn, batch_id, tenant_id, filename, report_type, as_of_date,
                  sha256, encoding, row_count) -> None:
    conn.execute(
        "INSERT INTO batches (batch_id, tenant_id, filename, report_type, as_of_date, "
        "sha256, detected_encoding, row_count, quarantine_count, status) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, 'loaded')",
        (batch_id, tenant_id, filename, report_type, as_of_date,
         sha256, encoding, row_count),
    )


def load_aging_summary_batch(conn, tenant_id: str, as_of_date: date,
                             summary: ParsedReport) -> tuple[uuid.UUID, bool]:
    _require_clean("aging summary", summary.quarantine)
    existing = _existing_batch(conn, tenant_id, summary.source_sha256)
    if existing is not None:
        return existing, False
    batch_id = uuid.uuid4()
    _insert_batch(conn, batch_id, tenant_id, Path(summary.source_path).name,
                  "aging_summary", as_of_date, summary.source_sha256,
                  summary.encoding, 6 * len(summary.summary_rows))
    for row in summary.summary_rows:
        customer_id = upsert_customer(conn, tenant_id, row.customer_key, row.customer_raw)
        pairs = (("Current", row.current), ("1 - 30", row.d1_30),
                 ("31 - 60", row.d31_60), ("61 - 90", row.d61_90),
                 ("> 90", row.over_90), ("Total", row.total))
        for bucket, amount in pairs:
            conn.execute(
                "INSERT INTO ar_aging_snapshots "
                "(tenant_id, batch_id, customer_id, bucket, amount, as_of_date) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (tenant_id, batch_id, customer_id, bucket, amount, as_of_date),
            )
    return batch_id, True


def _load_items_batch(conn, tenant_id, as_of_date, report, report_type, label):
    """Shared item-level load for Aging Detail and Open Invoices."""
    _require_clean(label, report.quarantine)
    existing = _existing_batch(conn, tenant_id, report.source_sha256)
    if existing is not None:
        return existing, False, {}
    batch_id = uuid.uuid4()
    _insert_batch(conn, batch_id, tenant_id, Path(report.source_path).name,
                  report_type, as_of_date, report.source_sha256,
                  report.encoding, len(report.open_items))
    customer_ids: dict[str, int] = {}
    for item in report.open_items:
        amount, direction = map_sign(item.txn_type, item.open_balance)
        customer_id = customer_ids.get(item.customer_key)
        if customer_id is None:
            customer_id = upsert_customer(conn, tenant_id, item.customer_key,
                                          item.customer_raw, item.terms or None)
            customer_ids[item.customer_key] = customer_id
        conn.execute(
            "INSERT INTO ar_transactions "
            "(tenant_id, batch_id, customer_id, invoice_number, txn_type, amount, direction, txn_date, qbd_bucket) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (tenant_id, batch_id, customer_id, item.num, item.txn_type,
             amount, direction, item.txn_date, item.bucket),
        )
    return batch_id, True, customer_ids


def load_aging_detail_batch(conn, tenant_id: str, as_of_date: date,
                            detail: ParsedReport) -> tuple[uuid.UUID, bool]:
    batch_id, loaded, customer_ids = _load_items_batch(
        conn, tenant_id, as_of_date, detail, "aging_detail", "aging detail")
    if not loaded:
        return batch_id, False
    for item in detail.open_items:
        if item.txn_type != "Invoice":
            continue
        computed = conn.execute(
            SIGNED_SUM_SQL + " AND invoice_number = %s AND txn_type = 'Invoice'",
            (tenant_id, batch_id, item.num),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO invoices "
            "(tenant_id, batch_id, invoice_number, customer_id, txn_date, due_date, "
            "terms, reported_balance, computed_balance) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (tenant_id, batch_id, item.num, customer_ids[item.customer_key],
             item.txn_date, item.due_date, item.terms, item.open_balance, computed),
        )
    return batch_id, True


def load_open_invoices_batch(conn, tenant_id: str, as_of_date: date,
                             open_invoices: ParsedReport) -> tuple[uuid.UUID, bool]:
    batch_id, loaded, _ = _load_items_batch(conn, tenant_id, as_of_date, open_invoices,
                                            "open_invoices", "open invoices")
    return batch_id, loaded


def load_cbd_batch(conn, tenant_id: str, as_of_date: date,
                   cbd: CBDReport) -> tuple[uuid.UUID, bool]:
    _require_clean("customer balance detail", cbd.quarantine)
    existing = _existing_batch(conn, tenant_id, cbd.source_sha256)
    if existing is not None:
        return existing, False
    batch_id = uuid.uuid4()
    _insert_batch(conn, batch_id, tenant_id, Path(cbd.source_path).name,
                  "customer_balance_detail", as_of_date, cbd.source_sha256,
                  cbd.encoding, len(cbd.endings))
    for normalized_name, amount in cbd.endings.items():
        # fallback for CBD-only customers: the frozen CBD parser exposes only normalized keys
        customer_id = upsert_customer(conn, tenant_id, normalized_name, normalized_name)
        conn.execute(
            "INSERT INTO ar_aging_snapshots "
            "(tenant_id, batch_id, customer_id, bucket, amount, as_of_date) "
            "VALUES (%s, %s, %s, 'Total', %s, %s)",
            (tenant_id, batch_id, customer_id, amount, as_of_date),
        )
    return batch_id, True


def load_snapshot(conn, tenant_id: str, as_of_date: date,
                  summary: ParsedReport, detail: ParsedReport,
                  open_invoices: ParsedReport, cbd: CBDReport,
                  ) -> dict[str, tuple[uuid.UUID, bool]]:
    """Load all four files atomically. Any LoadError rolls back everything."""
    with conn.transaction():
        results: dict[str, tuple[uuid.UUID, bool]] = {}
        results["aging_summary"] = load_aging_summary_batch(conn, tenant_id, as_of_date, summary)
        results["aging_detail"] = load_aging_detail_batch(conn, tenant_id, as_of_date, detail)
        results["open_invoices"] = load_open_invoices_batch(conn, tenant_id, as_of_date, open_invoices)
        results["customer_balance_detail"] = load_cbd_batch(conn, tenant_id, as_of_date, cbd)
    return results


# --- read-back: rebuild the gate's inputs purely from Postgres ---------------

def _batch_header(conn, tenant_id: str, batch_id):
    row = conn.execute(
        "SELECT report_type, filename, sha256, detected_encoding, as_of_date "
        "FROM batches WHERE tenant_id = %s AND batch_id = %s",
        (tenant_id, batch_id),
    ).fetchone()
    if row is None:
        raise LoadError("missing_batch", f"no batch {batch_id} for tenant {tenant_id!r}")
    return row


def readback_summary(conn, tenant_id: str, batch_id) -> ParsedReport:
    report_type, filename, sha, enc, as_of = _batch_header(conn, tenant_id, batch_id)
    rep = ParsedReport(report_type, str(as_of), filename, sha, enc)
    rows = conn.execute(
        "SELECT c.raw_name, c.normalized_name, s.bucket, s.amount "
        "FROM ar_aging_snapshots s "
        "JOIN customers c ON c.customer_id = s.customer_id "
        "WHERE s.tenant_id = %s AND s.batch_id = %s "
        "ORDER BY c.normalized_name",
        (tenant_id, batch_id),
    ).fetchall()
    raw_by_key: dict[str, str] = {}
    buckets_by_key: dict[str, dict[str, Decimal]] = {}
    for raw_name, key, bucket, amount in rows:
        raw_by_key[key] = raw_name
        buckets_by_key.setdefault(key, {})[bucket] = as_money(amount, f"summary {key} {bucket}")
    grand = Decimal("0.00")
    for key, buckets in buckets_by_key.items():
        missing = [b for b in (*BUCKETS, "Total") if b not in buckets]
        if missing:
            raise LoadError("snapshot_incomplete", f"customer {key!r} missing buckets {missing}")
        rep.summary_rows.append(AgingSummaryRow(
            customer_raw=raw_by_key[key], customer_key=key,
            current=buckets["Current"], d1_30=buckets["1 - 30"],
            d31_60=buckets["31 - 60"], d61_90=buckets["61 - 90"],
            over_90=buckets["> 90"], total=buckets["Total"],
        ))
        grand += buckets["Total"]
    rep.grand_total = grand
    return rep


def _readback_items(conn, tenant_id: str, batch_id, with_buckets: bool) -> ParsedReport:
    report_type, filename, sha, enc, as_of = _batch_header(conn, tenant_id, batch_id)
    rep = ParsedReport(report_type, str(as_of), filename, sha, enc)
    rows = conn.execute(
        "SELECT c.raw_name, c.normalized_name, t.txn_type, t.txn_date, "
        "COALESCE(t.invoice_number, ''), t.amount, t.direction, i.due_date, t.qbd_bucket "
        "FROM ar_transactions t "
        "JOIN customers c ON c.customer_id = t.customer_id "
        "LEFT JOIN invoices i ON i.tenant_id = t.tenant_id AND i.batch_id = t.batch_id "
        "AND i.invoice_number = t.invoice_number AND t.txn_type = 'Invoice' "
        "WHERE t.tenant_id = %s AND t.batch_id = %s "
        "ORDER BY t.txn_id",
        (tenant_id, batch_id),
    ).fetchall()
    grand = Decimal("0.00")
    for raw_name, key, txn_type, txn_date, num, amount, direction, due_date, qbd_bucket in rows:
        amount = as_money(amount, f"{report_type} item {num}")
        signed = amount if direction == "increase" else -amount
        # Aging is what QuickBooks reported and stored (qbd_bucket), never a
        # recomputation. The source system is authoritative; recomputing risks
        # disagreeing with the number the dashboard must tie to.
        bucket = qbd_bucket if with_buckets else None
        rep.open_items.append(OpenItem(
            customer_raw=raw_name, customer_key=key, txn_type=txn_type,
            txn_date=txn_date, num=num, po_number="", terms="",
            due_date=due_date, aging_days="", open_balance=signed,
            bucket=bucket, source_row_index=0,
        ))
        grand += signed
    rep.grand_total = grand
    return rep


def readback_detail(conn, tenant_id: str, batch_id) -> ParsedReport:
    return _readback_items(conn, tenant_id, batch_id, with_buckets=True)


def readback_open_invoices(conn, tenant_id: str, batch_id) -> ParsedReport:
    return _readback_items(conn, tenant_id, batch_id, with_buckets=False)


def readback_cbd(conn, tenant_id: str, batch_id) -> tuple[dict[str, Decimal], Decimal]:
    _batch_header(conn, tenant_id, batch_id)
    rows = conn.execute(
        "SELECT c.normalized_name, s.amount "
        "FROM ar_aging_snapshots s "
        "JOIN customers c ON c.customer_id = s.customer_id "
        "WHERE s.tenant_id = %s AND s.batch_id = %s AND s.bucket = 'Total'",
        (tenant_id, batch_id),
    ).fetchall()
    endings = {key: as_money(amount, f"cbd ending {key}") for key, amount in rows}
    total = sum(endings.values(), start=Decimal("0.00"))
    return endings, total


def canonical_ar_total(conn, tenant_id: str, detail_batch_id) -> Decimal:
    total = conn.execute(SIGNED_SUM_SQL, (tenant_id, detail_batch_id)).fetchone()[0]
    return as_money(total, "canonical AR total")


def run_canonical_gate(conn, tenant_id: str, batch_ids: dict) -> ReconReport:
    summary = readback_summary(conn, tenant_id, batch_ids["aging_summary"])
    detail = readback_detail(conn, tenant_id, batch_ids["aging_detail"])
    open_invoices = readback_open_invoices(conn, tenant_id, batch_ids["open_invoices"])
    endings, cbd_total = readback_cbd(conn, tenant_id, batch_ids["customer_balance_detail"])
    return reconcile_batch(summary, detail, open_invoices,
                           cbd_customer_endings=endings, cbd_grand_total=cbd_total)


# --- CLI ----------------------------------------------------------------------

def _find(directory: Path, *needles: str) -> Path | None:
    for p in sorted(directory.glob("*.csv")):
        name = p.name.lower()
        if all(n.lower() in name for n in needles):
            return p
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Load a QBD batch into canonical Postgres and rerun the gate from the database")
    parser.add_argument("directory", nargs="?", default="ips_files",
                        help="directory containing the four QBD CSV exports")
    parser.add_argument("--tenant", default="ips")
    parser.add_argument("--as-of", dest="as_of", default="2026-05-31",
                        help="snapshot as-of date, YYYY-MM-DD")
    args = parser.parse_args(argv)

    try:
        as_of = date.fromisoformat(args.as_of)
    except ValueError:
        print(f"invalid as-of date {args.as_of!r}, expected YYYY-MM-DD")
        return 1

    directory = Path(args.directory)
    summary_p = _find(directory, "aging", "summary")
    detail_p = _find(directory, "aging", "detail")
    oi_p = _find(directory, "openinvoices")
    cbd_p = _find(directory, "customerbalancedetail")
    missing = [label for label, p in
               [("Aging Summary", summary_p), ("Aging Detail", detail_p),
                ("Open Invoices", oi_p), ("Customer Balance Detail", cbd_p)] if p is None]
    if missing:
        print(f"Missing report files in {directory}: {', '.join(missing)}")
        return 1

    summary = parse_aging_summary(summary_p)
    detail = parse_aging_detail(detail_p)
    open_invoices = parse_open_invoices(oi_p)
    cbd = parse_customer_balance_detail(cbd_p)

    try:
        conn = get_conn()
    except LoadError as exc:
        print(str(exc))
        return 1
    except psycopg.OperationalError:
        print("could not connect to Postgres; check that the database is up "
              "and DATABASE_URL points at it")
        return 1

    with conn:
        apply_schema(conn)
        try:
            results = load_snapshot(conn, args.tenant, as_of,
                                    summary, detail, open_invoices, cbd)
        except LoadError as exc:
            print(f"LOAD REFUSED: {exc}")
            return 1

        batch_ids = {k: v[0] for k, v in results.items()}
        gate = run_canonical_gate(conn, args.tenant, batch_ids)
        ledger_total = canonical_ar_total(conn, args.tenant, batch_ids["aging_detail"])
        invoice_total = conn.execute(
            "SELECT count(*) FROM invoices WHERE tenant_id = %s AND batch_id = %s",
            (args.tenant, batch_ids["aging_detail"])).fetchone()[0]
        invoice_bad = conn.execute(
            "SELECT count(*) FROM invoices WHERE tenant_id = %s AND batch_id = %s AND delta <> 0",
            (args.tenant, batch_ids["aging_detail"])).fetchone()[0]

        bar = "=" * 70
        print(bar)
        print("CANONICAL POSTGRES RECONCILIATION REPORT")
        print(bar)
        for report_type, (batch_id, was_loaded) in results.items():
            row_count = conn.execute(
                "SELECT row_count FROM batches WHERE tenant_id = %s AND batch_id = %s",
                (args.tenant, batch_id)).fetchone()[0]
            tag = "loaded" if was_loaded else "no-op (already loaded)"
            print(f"  {report_type:26s} rows={row_count:<5d} {tag}")
            print(f"         batch {batch_id}")
        print(bar)
        print(f"AR control total (Aging Summary): {gate.ar_total}")
        print(f"Canonical ledger total (ar_transactions): {ledger_total}")
        print(f"Invoice deltas nonzero: {invoice_bad} of {invoice_total}")
        print(bar)
        for check in gate.checks:
            status = "PASS" if check.passed else "FAIL"
            dz = f"  (delta {check.delta})" if check.delta is not None else ""
            print(f"  [{status}] {check.name}")
            print(f"         {check.detail}{dz}")
        print(bar)
        verdict = ("CANONICAL GATE PASSED - dashboard may refresh" if gate.passed
                   else "CANONICAL GATE FAILED - refresh blocked")
        print(verdict)
        print(bar)
        return 0 if gate.passed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
