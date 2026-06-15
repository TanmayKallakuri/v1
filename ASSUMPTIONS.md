# Documented Assumptions

These are open client questions we could not get answered in time. For each, we
recorded the default decision we made, the reasoning behind it, and what to
revisit if the client's answer differs. Every item is marked ASSUMED, pending
client confirmation, so it reads as provisional, not as a confirmed fact.

Confirm all four with the client's bookkeeper when they are reachable. Of the
four, only assumption 2 (Account 28000) affects reconciliation logic. The other
three are scoping or presentation decisions that do not change the numbers the
gate ties to.

## 1. Cash account definition

- **Question:** Which general ledger accounts make up "Cash" for the Cash tile?
- **Decision:** Cash = Operating (10000) + Business Checking (10050) + Payroll
  (10100) only. Exclude Petty Cash (10200) and Undeposited Funds (12000).
- **Reasoning:** Those three are spendable bank balances. Undeposited Funds
  double-counts against the bank accounts and is not cash in hand. Petty Cash is
  immaterial. The conservative, narrower definition is the safer choice for a
  trust product. This only affects the Cash tile, which is deferred, so the
  decision is parked until cash is built.
- **Status:** ASSUMED, pending client confirmation.
- **Revisit if the answer differs:** If the client counts Petty Cash or
  Undeposited Funds as cash, widen the account set used by the Cash tile when it
  is built. No reconciliation impact and no change to AR.

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

## 4. Export cadence

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
