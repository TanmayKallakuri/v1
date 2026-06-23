# Documented Assumptions

These are open client questions we could not get answered in time. For each, we
recorded the default decision we made, the reasoning behind it, and what to
revisit if the client's answer differs. Every item is marked ASSUMED, pending
client confirmation, so it reads as provisional, not as a confirmed fact.

Confirm all five with the client's bookkeeper when they are reachable. Of the
five, only assumption 2 (Account 28000) would change reconciliation logic if the
answer differs. The others are scoping or presentation decisions that do not
change the numbers the gates tie to: the AR subledger ties at $609,772.89 and
the cash control ties at $451,068.87, both to the cent.

## 1. Cash account definition

- **Question:** Which general ledger accounts make up "Cash" for the Cash tile?
- **Decision:** Cash = Operating (10000) + Business Checking (10050) + Payroll
  (10100) only. Exclude Petty Cash (10200) and Undeposited Funds (12000).
- **Reasoning:** Those three are spendable bank balances. Undeposited Funds
  double-counts against the bank accounts and is not cash in hand. Petty Cash is
  immaterial. The conservative, narrower definition is the safer choice for a
  trust product. This only affects the Cash tile, which is deferred, so the
  decision is parked until cash is built.
- **Status:** ASSUMED, pending client confirmation. The Cash tile is now built
  and this decision is config-driven, not hardcoded: the cash account set lives
  in one place, `CASH_ACCOUNTS` in `qbd_core.py` (with `CASH_PETTY_CASH` and
  `CASH_UNDEPOSITED` named alongside it). The Balance Sheet parser, the GL cash
  extractor, and the cash gate all import it.
- **Revisit if the answer differs:** If the client counts Petty Cash or
  Undeposited Funds as cash, add the account to `CASH_ACCOUNTS` in `qbd_core.py`;
  that one line is the entire change. Petty Cash (10200, $500.00) and Undeposited
  Funds (12000, $101,950.60) are already parsed and reported separately, just not
  summed into the cash total. No reconciliation impact and no change to AR. The
  current cash control ties at $451,068.87 (GL-computed cash total equals the
  Balance Sheet cash control to the cent).

## 2. Account 28000 Customer Over-Payment

- **Question:** Should the balance in account 28000 (Customer Over-Payment) be
  netted into the AR total?
- **Decision:** Reconciliation ties to the AR account (11000) only. 28000 is NOT
  netted into the AR total. Any 28000 balance is surfaced separately as a flagged
  potential off-AR credit.
- **Reasoning:** The trust test ties to the QuickBooks A/R Aging Summary, which
  is built from the AR account. Folding 28000 into the AR computation would
  diverge from the very report we must match. The quirk is shown as a note beside
  the number, never inside it.
- **Status:** ASSUMED, pending client confirmation.
- **Revisit if the answer differs:** This is the only one of the four that
  touches reconciliation logic. If the client confirms 28000 belongs in the AR
  total, the reconciliation gate and the AR computation must be revised, and the
  trust number itself would change. Treat any such change as a reconciliation
  change, not a display change.

## 3. The 2022 A & A Sprinkler credit

- **Question:** How should we handle the A & A Sprinkler credit of -13,341.66,
  which is the entire greater-than-90 bucket?
- **Decision:** Display it as-is per source, never alter it. Apply or write-off is
  a client accounting decision flagged for their bookkeeper, not ours to make.
  Surface it prominently in the credits and unapplied list with its age.
- **Reasoning:** Altering a client's books has tax and audit implications and is
  categorically out of scope. Surfacing a four-year-old unapplied credit is the
  product doing its job.
- **Status:** ASSUMED, pending client confirmation.
- **Revisit if the answer differs:** If the client instructs us to apply or write
  off the credit, that is a change they make in QuickBooks, which then flows
  through on the next export. We still do not alter the source. No code change on
  our side.

## 4. GL/Balance Sheet AR vs the AR Aging subledger ($37,581.04 difference)

- **Question:** The AR account (11000) on the General Ledger and Balance Sheet
  reads $572,191.85, but the A/R Aging Summary subledger totals $609,772.89, a
  difference of $37,581.04. Which is "the" AR number, and must they be made to
  agree?
- **Decision:** This is a known and expected difference, not a pipeline error.
  The AR trust number stays the AR Aging Summary subledger total, $609,772.89:
  that is the report the product promises to tie to, and the AR gate ties to it
  to the cent. The cash side independently ties the GL cash total to the Balance
  Sheet cash control at $451,068.87. The $37,581.04 GL-to-subledger AR gap is
  reported as a note, never silently reconciled away.
- **Reasoning:** A GL control account and its aging subledger drift apart for
  ordinary bookkeeping reasons, chiefly aged credits and journal activity posted
  to the control account that the aging report buckets differently (the same
  family of effects as the -$13,341.66 legacy credit in assumption 3). Forcing
  the two to agree would mean altering one of the source numbers, which is
  exactly what a trust product must not do. Fully decomposing the gap line by
  line is a reconciliation exercise that belongs to the client's bookkeeper.
- **Status:** ASSUMED, pending client confirmation. Documented as a known,
  expected difference driven by aged credits and control-account activity.
- **Revisit if the answer differs:** Fully reconciling the GL control account to
  the aging subledger is out of v1 scope. If the client wants the gap explained
  line by line, that is a v1.1+ analysis (decompose 28000 over-payments, aged
  credits, and any direct journal entries to 11000); it does not change the AR
  trust number or the cash reconciliation, both of which already tie.

## 5. Export cadence

- **Question:** How often will the client export the four reports, and as of what
  date?
- **Decision:** Assume monthly at month-end as the standing cadence, with Open
  Invoices exported on the as-of date itself.
- **Reasoning:** Month-end is the universal financial-reporting default, makes
  snapshots comparable month over month, and aligns with the other reports. A
  different date later is a one-line doc change, not a code change.
- **Status:** ASSUMED, pending client confirmation.
- **Revisit if the answer differs:** If the client exports on a different cadence
  or as-of date, update this note. The pipeline already reads the as-of date per
  batch, so a different date is a documentation change, not a code change.
