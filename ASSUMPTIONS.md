# Documented Assumptions

These were open client questions we could not get answered in time. For each, we
recorded the default decision we made, the reasoning behind it, and what to
revisit if the client's answer differs. The client's bookkeeper (Maria) has now
answered all four, forwarded by Cezar on June 23, 2026, so the status of each is
updated below from assumed to confirmed. One item, the cash account definition,
is confirmed in direction but still has a final figure pending Cezar's
confirmation.

Of the four, only assumption 2 (Account 28000) touches reconciliation logic, and
it is confirmed with no pipeline impact. The other three are scoping or
presentation decisions that do not change the numbers the gate ties to.

## 1. Cash account definition

- **Question:** Which general ledger accounts make up "Cash" for the Cash tile?
- **Decision:** Cash = Operating (10000) + Business Checking (10050) + Payroll
  (10100) only. Exclude Petty Cash (10200) and Undeposited Funds (12000).
- **Reasoning:** Those three are spendable bank balances. Undeposited Funds
  double-counts against the bank accounts and is not cash in hand. Petty Cash is
  immaterial. The conservative, narrower definition is the safer choice for a
  trust product. This only affects the Cash tile, which is deferred, so the
  decision is parked until cash is built.
- **Status:** Client leans toward all cash accounts; final figure pending Cezar's
  confirmation. The client said keep all cash accounts ("All Cash accounts should
  stay, or we can decide later"), but Maria was tentative and the figure moves
  materially: 3 bank accounts total $451,068.87 versus all cash accounts (adding
  Petty Cash and Undeposited Funds) at $553,519.47. Pipeline currently set to 3
  banks ($451,068.87); changing to all accounts is a one-line `CASH_ACCOUNTS`
  config change plus updating the gate target to $553,519.47.
- **Revisit if the answer differs:** Once Cezar confirms the final figure, if it
  is all cash accounts, add Petty Cash and Undeposited Funds to `CASH_ACCOUNTS`
  and move the cash gate target to $553,519.47. No reconciliation impact on AR
  either way.

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
- **Status:** CONFIRMED, no pipeline impact. The client confirmed account 28000
  can be eliminated and does not need special handling. No pipeline change is
  required: we already keep it out of the AR total and surface any balance
  separately.
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
- **Status:** CONFIRMED for source-side elimination; pipeline must NOT be
  modified to remove it. The client wants the credit eliminated, but this must
  happen in QuickBooks at the source, not in our pipeline.
- **Warning:** This credit is part of the $609,772.89 that ties to the QuickBooks
  A/R Aging Summary. Removing it from our pipeline would break the reconciliation
  gate. The correct sequence is: the client eliminates or applies it in
  QuickBooks, re-exports, and our pipeline ties to the new lower total
  automatically. Never strip it on our side.
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
- **Status:** CONFIRMED. The client confirmed monthly at month-end, with Open
  Invoices exported on the as-of date. This matches the existing START HERE doc.
- **Revisit if the answer differs:** If the client exports on a different cadence
  or as-of date, update this note. The pipeline already reads the as-of date per
  batch, so a different date is a documentation change, not a code change.
