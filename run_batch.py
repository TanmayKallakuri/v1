"""
run_batch.py - parse a full QBD batch and run the reconciliation gate.

Usage:
    python3 run_batch.py [directory_with_the_four_csvs]

Looks for the four standard reports by filename signature, parses each, runs the
six-check gate, and prints a demo-ready report. Exit code is 0 when the gate
passes, 1 when it fails, mirroring how the pipeline blocks a dashboard refresh.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

from qbd_reports import (
    parse_aging_summary,
    parse_aging_detail,
    parse_open_invoices,
    unmapped_types,
)
from qbd_reconcile import reconcile_batch
from qbd_cbd import parse_customer_balance_detail, customer_endings, grand_total as cbd_grand


def _find(directory: Path, *needles: str) -> Path | None:
    for p in directory.glob("*.csv"):
        name = p.name.lower()
        if all(n.lower() in name for n in needles):
            return p
    return None


def run(directory: str | Path) -> int:
    directory = Path(directory)
    summary_p = _find(directory, "aging", "summary")
    detail_p = _find(directory, "aging", "detail")
    oi_p = _find(directory, "openinvoices")
    cbd_p = _find(directory, "customerbalancedetail")

    missing = [n for n, p in
               [("Aging Summary", summary_p), ("Aging Detail", detail_p),
                ("Open Invoices", oi_p), ("Customer Balance Detail", cbd_p)] if p is None]
    if missing:
        print(f"Missing report files: {', '.join(missing)}")
        return 1

    summary = parse_aging_summary(summary_p)
    detail = parse_aging_detail(detail_p)
    oi = parse_open_invoices(oi_p)
    cbd = parse_customer_balance_detail(cbd_p)

    cbd_endings = customer_endings(cbd)
    rr = reconcile_batch(
        summary, detail, oi,
        cbd_customer_endings=cbd_endings,
        cbd_grand_total=cbd_grand(cbd),
    )

    bar = "=" * 70
    print(bar)
    print("QBD BATCH RECONCILIATION REPORT")
    print(bar)
    for rep, label in [(summary, "Aging Summary"), (detail, "Aging Detail"),
                       (oi, "Open Invoices"), (cbd, "Customer Balance Detail")]:
        q = len(rep.quarantine)
        enc = rep.encoding
        gt = rep.grand_total if rep.grand_total is not None else "n/a"
        print(f"  {label:26s} enc={enc:6s} grand_total={gt:>14} quarantine={q}")

    # Surface any unmapped transaction types (would quarantine in strict mode).
    um = unmapped_types(detail) | unmapped_types(oi)
    if um:
        print(f"  UNMAPPED transaction types: {um}")
    print(bar)
    print(f"AR control total (Aging Summary): {rr.ar_total}")
    print(bar)
    for c in rr.checks:
        status = "PASS" if c.passed else "FAIL"
        dz = f"  (delta {c.delta})" if c.delta is not None else ""
        print(f"  [{status}] {c.name}")
        print(f"         {c.detail}{dz}")
    print(bar)
    verdict = "GATE PASSED - dashboard may refresh" if rr.passed else "GATE FAILED - refresh blocked"
    print(verdict)
    print(bar)
    return 0 if rr.passed else 1


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "."
    raise SystemExit(run(target))
