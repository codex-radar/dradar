# Interrupts and one-session crash recovery

Ctrl+C asks the signed CLI worker to finish container cleanup and durable
session close before its launcher returns (up to 120 seconds). A second Ctrl+C
is deferred during that cleanup. Task files and pending uploads remain on disk.
If exit cannot be established, capacity stays reserved.

On the **original Linux host**, using the original account configuration or
saved `--plan RUN_CODE`, inspect one session:

```sh
dradar capacity --recover-session SESSION_ID --json
```

This reads the journal, original process identities, original Docker daemon,
exact-job Compose containers and authenticated session receipt. It does not
signal processes, remove containers, close sessions, alter assignments or
create a local lock file. Finish the original runtime’s normal cleanup before
recovery. Exact-job containers must be absent, including stopped containers;
this entrance does not remove them or reinterpret stopped as absent.

For a `ready` result, use its exact `journal_sha256`:

```sh
dradar capacity --recover-session SESSION_ID --execute --journal-sha256 SHA256 --json
```

Execution repeats inspection under the original journal lock. It preserves an
original `.before-recovery` snapshot and appends a distinct `recovered_absent`
observation; it does not replace the historical `spawned` event. It then seals
and retries the same close/release evidence against the exact server receipt.
The original digest may be reused after a lost response; concurrent invocations
serialize and reuse the same release evidence. `receipt_pending` is not success.

This command only releases session capacity. It does not restart work, return a
held assignment, upload a result, or change another assignment/session. Keep
original job files and pending upload records for their normal recovery path.

Missing launch/host/boot/daemon evidence, a present or reused PID, surviving
process group/container, Docker failure, changed journal or foreign identity
leaves exit `unknown`. Historical journals without the original identities are
not backfilled. Windows/macOS crash recovery and independent managed host helper
groups are not supported by this entrance; their normal interrupt paths remain.
