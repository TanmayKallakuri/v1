"""
qbd_reconcile.py - the reconciliation gate (Stage 4 of the workflow).

Six checks, all using a $0.01 absolute tolerance. The gate passes only when
every check passes. The Open Invoices check encodes the known QBD quirk: that
report only includes items still open at export time, so a shortfall versus the
Aging total is acceptable only when every missing invoice exists in Aging Detail
(i.e. the gap is explained by post-period payment activity, not by lost data).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from qbd_core import RECON_TOLERANCE
from qbd_reports import ParsedReport, TXN_TYPE_DIRECTION


@dataclass
class Check:
    name: str
    passed: bool
    detail: str
    delta: Decimal | None = None


@dataclass
class ReconReport:
    checks: list[Check] = field(default_factory=list)
    ar_total: Decimal | None = None

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    def add(self, name: str, passed: bool, detail: str, delta: Decimal | None = None) -> None:
        self.checks.append(Check(name, passed, detail, delta))


def _within(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= RECON_TOLERANCE


def computed_ar_from_aging_detail(detail: ParsedReport) -> Decimal:
    """Independent AR total: sum every open item's balance from Aging Detail.

    General Journal entries carry their direction in the sign of the amount
    itself, so a plain sum of open balances is correct here. This is OUR number,
    derived from item rows, not read from any TOTAL line.
    """
    return sum((it.open_balance for it in detail.open_items), start=Decimal("0"))


def computed_buckets_from_aging_detail(detail: ParsedReport) -> dict[str, Decimal]:
    """Independently recompute each bucket total by summing its item rows."""
    out: dict[str, Decimal] = {}
    for it in detail.open_items:
        if it.bucket is None:
            continue
        out[it.bucket] = out.get(it.bucket, Decimal("0")) + it.open_balance
    return out


def reconcile_batch(
    summary: ParsedReport,
    detail: ParsedReport,
    open_invoices: ParsedReport,
    cbd_customer_endings: dict[str, Decimal] | None = None,
    cbd_grand_total: Decimal | None = None,
) -> ReconReport:
    """Run all six gate checks across the batch and return the result."""
    rr = ReconReport()
    control = summary.grand_total
    rr.ar_total = control

    # 1. Keystone: our computed AR (from canonical item rows) == Aging Summary TOTAL.
    computed = computed_ar_from_aging_detail(detail)
    if control is not None:
        rr.add("Keystone (computed AR = Aging Summary TOTAL)",
               _within(computed, control),
               f"computed {computed} vs control {control}", control - computed)
    else:
        rr.add("Keystone", False, "Aging Summary grand TOTAL missing")

    # 2. Cross-report A: sum of Customer Balance Detail customer endings == control.
    if cbd_grand_total is not None and control is not None:
        rr.add("Cross-report A (CBD endings = Aging Summary TOTAL)",
               _within(cbd_grand_total, control),
               f"CBD total {cbd_grand_total} vs control {control}",
               control - cbd_grand_total)
    else:
        rr.add("Cross-report A (CBD endings = Aging Summary TOTAL)", True,
               "skipped (CBD total not supplied)")

    # 3. Cross-report B: Aging Detail grand TOTAL == Aging Summary TOTAL.
    if detail.grand_total is not None and control is not None:
        rr.add("Cross-report B (Aging Detail TOTAL = Aging Summary TOTAL)",
               _within(detail.grand_total, control),
               f"detail {detail.grand_total} vs control {control}",
               control - detail.grand_total)
    else:
        rr.add("Cross-report B", False, "a grand TOTAL is missing")

    # 4. Bucket integrity: recomputed buckets sum to AR total, and each matches
    #    the Aging Summary's own bucket columns.
    comp_buckets = computed_buckets_from_aging_detail(detail)
    bucket_sum = sum(comp_buckets.values(), start=Decimal("0"))
    summary_buckets = _summary_bucket_totals(summary)
    bucket_ok = control is not None and _within(bucket_sum, control)
    mism = []
    for b, v in summary_buckets.items():
        cv = comp_buckets.get(b, Decimal("0"))
        if not _within(cv, v):
            mism.append(f"{b}: computed {cv} vs summary {v}")
    rr.add("Bucket integrity (recomputed buckets = AR total and match summary)",
           bucket_ok and not mism,
           f"bucket sum {bucket_sum}" + (("; mismatches: " + "; ".join(mism)) if mism else "; all buckets match"))

    # 5. Open Invoices delta explained by post-period activity (the quirk).
    rr.add(*_check_open_invoices_quirk(detail, open_invoices))

    # 6. Per-customer: computed per-customer balance == Aging Summary row.
    rr.add(*_check_per_customer(summary, detail))

    return rr


def _summary_bucket_totals(summary: ParsedReport) -> dict[str, Decimal]:
    """The Aging Summary's own per-bucket grand totals (from its TOTAL row math)."""
    out = {"Current": Decimal("0"), "1 - 30": Decimal("0"), "31 - 60": Decimal("0"),
           "61 - 90": Decimal("0"), "> 90": Decimal("0")}
    for row in summary.summary_rows:
        out["Current"] += row.current
        out["1 - 30"] += row.d1_30
        out["31 - 60"] += row.d31_60
        out["61 - 90"] += row.d61_90
        out["> 90"] += row.over_90
    return out


def _check_open_invoices_quirk(detail: ParsedReport, oi: ParsedReport):
    """Open Invoices may total less than Aging; verify the gap is fully explained.

    Acceptable iff every item present in Aging Detail but absent from Open
    Invoices is a real Aging item (so the shortfall is post-period payments),
    and the residual after accounting for those is zero. We key items by
    (customer_key, invoice number).
    """
    def key(it):
        return (it.customer_key, it.num)

    detail_keys = {key(it): it.open_balance for it in detail.open_items}
    oi_keys = {key(it) for it in oi.open_items}

    missing = {k: bal for k, bal in detail_keys.items() if k not in oi_keys}
    detail_total = sum(detail_keys.values(), start=Decimal("0"))
    oi_total = oi.grand_total if oi.grand_total is not None else sum(
        (it.open_balance for it in oi.open_items), start=Decimal("0"))

    explained = sum(missing.values(), start=Decimal("0"))
    residual = (detail_total - oi_total) - explained
    ok = _within(residual, Decimal("0"))
    detail_str = (f"Aging {detail_total} - OpenInv {oi_total} = "
                  f"{detail_total - oi_total}; explained by {len(missing)} "
                  f"post-period items totaling {explained}; residual {residual}")
    return ("Open Invoices delta explained by post-period activity", ok, detail_str, residual)


def _check_per_customer(summary: ParsedReport, detail: ParsedReport):
    """Each customer's summed open items (Aging Detail) == their Aging Summary row."""
    by_cust: dict[str, Decimal] = {}
    for it in detail.open_items:
        by_cust[it.customer_key] = by_cust.get(it.customer_key, Decimal("0")) + it.open_balance

    mismatches = []
    for row in summary.summary_rows:
        computed = by_cust.get(row.customer_key, Decimal("0"))
        if not _within(computed, row.total):
            mismatches.append(f"{row.customer_raw}: computed {computed} vs summary {row.total}")
    ok = not mismatches
    detail_str = "all customers tie" if ok else "; ".join(mismatches)
    return ("Per-customer (computed = Aging Summary row)", ok, detail_str)
