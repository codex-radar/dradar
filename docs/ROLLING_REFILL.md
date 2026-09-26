# Explicit rolling refill

The default `seed_barrier` mode waits for every initially selected assignment
to be submitted before claiming replacements. Existing plans keep that behavior.

A website run plan may explicitly authorize `rolling_submitted`. The official
`dradar run --plan ...` command carries that mode through Fleet. The advanced
Fleet command also accepts `--refill-mode rolling-submitted` with `--refill`,
an exact campaign scope, and a total task limit. The Server still requires the
active plan's device credential; an account token cannot grant this authority.
The CLI checks read-only rolling capabilities before configuring a campaign.

Each accepted submission provides at most one replacement claim. Upload attempts,
local pending files, grading progress, and duplicate acknowledgements do not
provide credits. The Server checks credits, held queue target, cumulative task
limit, scope, faults, and account limits in the transaction that creates the
replacement. A successful claim may wait in the held queue until execution has
capacity. Submission itself never releases an unknown runner reservation.

`--held-only` disables refill even when the original plan enabled rolling.
It does not switch a stopped campaign back to active. Provider quota exhaustion
and local stop continue to block new execution. Saved results retain the same
upload protection and original credentials as other run plans.

Rolling status comes from the Server. Missing mode, invalid credit counts, or an
inconsistent credit ledger prevent new claims. Lost claim responses preserve the
unknown outcome; callers must reconcile existing work before trying again.

This candidate is under isolated acceptance. Local tests do not demonstrate
production recovery, physical provider concurrency, or remaining provider quota.
