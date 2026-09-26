# Run-plan intents

The official `run`, `stop`, and `progress` commands use `run-plan-intents-v1`
with the existing runner reservation protocol. New execution requires the
Server's intent CAS and admission-heartbeat capabilities.

## User flow

- `dradar run --plan <RUN_CODE>` observes the exact device, preserves saved
  results and unresolved reservations, and submits one durable start intent.
  Explicit resume may exchange a new credential after reconciling the original
  records. Old credentials remain available for their exact results and receipts.
- `dradar stop --plan <RUN_CODE>` first records local cancellation and publishes
  drain. It then requests a Server stop at one observed revision. Already admitted
  work may finish; stop does not prove process exit or release capacity.
- `dradar progress --plan <RUN_CODE>` reads pending intent receipts and reports
  the current plan. It cannot start or resume a runner.
- Repeating `run` for the same live local pool only maintains its exact admission
  and reads progress. It does not create a new start or change the pool's revision.

All-device stop retains the official confirmation flow. A changed device scope
invalidates its confirmation. Use the returned current choices; a stale token
does not authorize stopping a different admission.

## Durable operation and lost response

Before transmission, the CLI saves a private record under
`$DRADAR_HOME/run-plans/remote-intents/`. It contains an immutable ID, request,
fingerprint, original credential scope and local operation identity. These records
must be preserved with the private credentials and saved results.

A lost response is reconciled by GET for the original ID and fingerprint. An
unknown receipt remains unknown. A later explicit retry may resend that same ID
only when its original parameters and revision still match. A CAS conflict does
not refresh the revision, remove protocol fields, or silently retry with a new ID.
Pending operations block a replacement, including after a token change.

An old applied receipt proves a historical operation. Launch still requires a
fresh effective admission and the unchanged local lifecycle. A recovered stop
receipt is reported as confirmed only while it remains effective. Local cancellation
and an unknown or rejected remote stop are reported separately.

Known capacity rejection may permit the existing bounded capacity recheck at the
same revision. That business decision is saved before a new operation is created.
Unknown outcomes and authority conflicts do not enter this path.

## Local ordering and idle pools

Local stop publication and launch permission share a short file lock. Stop records
its lifecycle snapshot while holding that lock; network calls occur afterward.
Responses cannot overwrite or drain a later local lifecycle. Fleet stores the
original admission generation/revision, so an old process failure cannot adopt
a newer observation from a mutable credential file.

An empty rolling-refill pool uses the narrow heartbeat with its original admission
ID, revision and credential generation. A conflicting heartbeat cannot reactivate
the device or refresh its authority. Capacity remains governed by durable session
reservations and exact exit evidence.

## Compatibility and recovery limits

Legacy stop remains available for an unmigrated legacy scope. Modern admissions
require intent fields; an old client cannot downgrade that scope. A modern client
does not start against a Server lacking the required capabilities. Existing exact
result upload and cleanup retain their original scope checks.

Rolling back to a binary that ignores intent revisions requires fencing new
start/stop writes at the service boundary. Keep intent receipts, generations,
reservations and saved results. These protocol checks do not establish physical
provider exit or validate production inventory.
