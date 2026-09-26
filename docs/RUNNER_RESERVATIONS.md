# Stop, resume, and capacity receipts

This candidate requires a Server that advertises `runner-reservation-v1` before
new execution. Read, stop, and saved-result recovery remain separate operations.
It has not been released or accepted for production.

## User-visible behavior

- `dradar stop --plan <original-run-code> --scope this-device --json` records
  local cancellation before waiting for network responses. This also works while
  the first run is exchanging its invitation. If the plan identity is not yet
  available, the response says that local cancellation is recorded and the remote
  stop is unconfirmed. It does not claim that active processes have exited.
- A delayed run, automatic recheck, queued Fleet launch, or provider permission
  from the cancelled local generation cannot create another launch. An explicit
  new run can request a new generation after old execution has been reconciled.
- `run --upload-only` can finish exact saved results with retained original
  credentials. A credential exchange does not overwrite an older Fleet's private
  credential file. Damaged or unmatched results remain protected for review.
- A close acknowledgement is a logical session fence. Capacity stays reserved
  until the CLI has retained evidence that every execution in that session has
  ended. Expiry, stale heartbeat, a new owner epoch, or a missing parent PID alone
  cannot release it.
- Run, upload-only, and progress retry sealed capacity evidence. Lost close or
  release acknowledgements reuse the same session and evidence ID, and check the
  exact read-only receipt. They do not create replacement sessions to recover a
  lost receipt.

## Local evidence and failure handling

The private `runner-reservations` directory under `DRADAR_HOME` contains one
durable journal per runner session. It is created before that session can register.
Each attempt is recorded before entering `run_trial`; the real runner reports a
bound execution ID and ordered launch/exit events. An external adapter that does
not report evidence leaves an unknown attempt. It is never assumed to have exited.

The runner revokes provider permission files and audits its private POSIX process
group, then removes only containers attributed to that exact job and performs a
fresh Docker absence check. A failed Docker query is unknown. Normal return,
cancellation, registration failure, and cleanup failure all retain their distinct
outcomes. A registration close request is sent after local cleanup and is not used
as the physical-exit test.

A session is sealed before its logical close. No sealed session accepts new
attempts. Only a complete empty journal or a journal whose every attempt has
explicit no-launch/exit evidence can prepare a release request. The request and
its digest are saved before sending. Missing, corrupted, incomplete, mismatched,
or altered evidence prevents automatic release. Do not delete a journal, edit its
confirmation fields, or clear a server reservation to make admission succeed.

## Boundaries still requiring acceptance

The process-group audit covers the trusted runner's managed POSIX group and exact
job containers. It does not prove containment of arbitrary detached host children.
Native Windows and the managed-auth path with independent host groups currently
remain unknown until their containment evidence is integrated; they cannot claim
automatic capacity release from this audit. Cross-restart cleanup of an incomplete
execution journal requires exact resource/ownership evidence and is not inferred
from a reusable PID or an empty directory.

Legacy Server sessions have no new journal. Server migration quarantines unknown
history and requires scoped inventory and an explicit reconciliation declaration.
The wire API is available, but this candidate does not automatically manufacture
that historical proof. Before release, review the real inventory and the remaining
platform/recovery limitations, verify a clean Mac journey, and run the authorized
end-to-end acceptance. Isolated tests are not evidence of actual provider concurrency.
