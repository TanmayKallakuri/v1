"""
cash_reconcile.py - the cash reconciliation gate.

Mirrors the AR gate (qbd_reconcile.py): a small set of checks, all using the
same $0.01 absolute tolerance, with the gate passing only when every check
passes. The keystone is the trust test for the Cash tile: the GL-computed cash
total (opening + period transactions, summed over the in-scope cash accounts)
must equal the Balance Sheet cash control total to the cent. The current
snapshot's trust number is 451,068.87.

A failed cash gate is a hard stop, exactly like AR: it blocks the serving
refresh and keeps the last good snapshot live.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from qbd_core import RECON_TOLERANCE
from qbd_reconcile import Check


@dataclass
class CashReconReport:
    checks: list[Check] = field(default_factory=list)
    cash_total: Decimal | None = None        # GL-computed cash total (our number)
    cash_control: Decimal | None = None       # Balance Sheet cash control total

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    def add(self, name: str, passed: bool, detail: str, delta: Decimal | None = None) -> None:
        self.checks.append(Check(name, passed, detail, delta))


def _within(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= RECON_TOLERANCE


def reconcile_cash(gl_cash_by_account: dict[str, Decimal],
                   bs_cash_by_account: dict[str, Decimal]) -> CashReconReport:
    """Run the cash gate over per-account GL endings and Balance Sheet balances.

    Inputs are plain dicts (account_number -> Decimal) so the same function runs
    against freshly parsed files and against rows read back from Postgres,
    unmodified, the way the AR gate does.
    """
    rr = CashReconReport()
    gl_total = sum(gl_cash_by_account.values(), start=Decimal("0"))
    bs_total = sum(bs_cash_by_account.values(), start=Decimal("0"))
    rr.cash_total = gl_total
    rr.cash_control = bs_total

    # 1. Keystone: GL-computed cash total == Balance Sheet cash control.
    rr.add("Keystone (GL cash total = Balance Sheet cash control)",
           _within(gl_total, bs_total),
           f"GL computed {gl_total} vs Balance Sheet control {bs_total}",
           bs_total - gl_total)

    # 2. Account coverage: the same cash accounts appear on both sides. A bank
    #    account present in one source but not the other is a real discrepancy,
    #    not a rounding gap, so it must fail loudly rather than net to zero.
    gl_keys = set(gl_cash_by_account)
    bs_keys = set(bs_cash_by_account)
    missing_gl = bs_keys - gl_keys
    missing_bs = gl_keys - bs_keys
    coverage_ok = not missing_gl and not missing_bs
    if coverage_ok:
        coverage_detail = f"all {len(gl_keys)} cash accounts present on both sides"
    else:
        coverage_detail = (f"accounts only on Balance Sheet: {sorted(missing_gl)}; "
                           f"accounts only in GL: {sorted(missing_bs)}")
    rr.add("Account coverage (same cash accounts on both sides)",
           coverage_ok, coverage_detail)

    # 3. Per-account: each cash account's GL ending == its Balance Sheet balance.
    mismatches = []
    for num in sorted(gl_keys & bs_keys):
        gl_amt = gl_cash_by_account[num]
        bs_amt = bs_cash_by_account[num]
        if not _within(gl_amt, bs_amt):
            mismatches.append(f"{num}: GL {gl_amt} vs Balance Sheet {bs_amt}")
    rr.add("Per-account (each GL ending = Balance Sheet balance)",
           not mismatches,
           "all cash accounts tie" if not mismatches else "; ".join(mismatches))

    return rr
