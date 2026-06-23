"""
run_cash.py - parse the Balance Sheet and General Ledger and run the cash gate.

Usage:
    python3 run_cash.py [directory_with_the_two_xlsx]   (default: cash_files)

Finds the Balance Sheet and General Ledger exports by filename signature,
parses each, computes the GL cash total and the Balance Sheet cash control, runs
the cash reconciliation gate, and prints a demo-ready report. Exit code is 0
when the gate passes, 1 when it fails, mirroring how the pipeline blocks a
dashboard refresh. The current snapshot ties at 451,068.87.
"""

from __future__ import annotations

import sys
from pathlib import Path

from qbd_balance_sheet import parse_balance_sheet
from qbd_gl_cash import parse_general_ledger
from cash_reconcile import reconcile_cash


def _find(directory: Path, *needles: str) -> Path | None:
    for p in sorted(directory.glob("*.xlsx")):
        name = p.name.lower()
        if all(n.lower() in name for n in needles):
            return p
    return None


def run(directory: str | Path) -> int:
    directory = Path(directory)
    bs_p = _find(directory, "bs")
    if bs_p is None:
        bs_p = _find(directory, "balance")
    gl_p = _find(directory, "general", "ledger")
    if gl_p is None:
        gl_p = _find(directory, "gl")

    missing = [n for n, p in [("Balance Sheet", bs_p), ("General Ledger", gl_p)] if p is None]
    if missing:
        print(f"Missing cash source files in {directory}: {', '.join(missing)}")
        return 1

    bs = parse_balance_sheet(bs_p)
    gl = parse_general_ledger(gl_p)

    gl_by_acct = gl.cash_by_account()
    bs_by_acct = bs.cash_by_account()
    rr = reconcile_cash(gl_by_acct, bs_by_acct)

    bar = "=" * 70
    print(bar)
    print("QBD CASH RECONCILIATION REPORT")
    print(bar)
    print(f"  Balance Sheet  as_of={bs.as_of_label!r} ({bs.as_of_date}) "
          f"leaf_accounts={len(bs.accounts)} quarantine={len(bs.quarantine)}")
    print(f"  General Ledger accounts_landed={len(gl.all_accounts)} "
          f"cash_accounts_promoted={len(gl.cash)}")
    print(bar)
    print("  Cash accounts (assumption #1: three bank accounts):")
    for num in sorted(gl.cash):
        a = gl.cash[num]
        bs_amt = bs_by_acct.get(num)
        print(f"    {num}  {a.name:<34} GL ending {a.ending:>13}  "
              f"Balance Sheet {bs_amt if bs_amt is not None else 'n/a':>13}  ({len(a.txns)} txns)")
    print(f"  Excluded (parsed, not counted): "
          f"Petty Cash {[ (p.number, str(p.amount)) for p in bs.petty() ]}, "
          f"Undeposited Funds {[ (u.number, str(u.amount)) for u in bs.undeposited() ]}")
    print(bar)
    print(f"GL-computed cash total      : {rr.cash_total}")
    print(f"Balance Sheet cash control  : {rr.cash_control}")
    print(bar)
    for c in rr.checks:
        status = "PASS" if c.passed else "FAIL"
        dz = f"  (delta {c.delta})" if c.delta is not None else ""
        print(f"  [{status}] {c.name}")
        print(f"         {c.detail}{dz}")
    print(bar)
    verdict = "CASH GATE PASSED - dashboard may refresh" if rr.passed else "CASH GATE FAILED - refresh blocked"
    print(verdict)
    print(bar)
    return 0 if rr.passed else 1


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "cash_files"
    raise SystemExit(run(target))
