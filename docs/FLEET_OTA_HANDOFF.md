# Bounded Fleet stop and signed update handoff

Linux Fleet already has an explicit stop route. It does not automatically
drain a live campaign when an update is staged. An operator with permission
for the exact HOME and batches can use the following existing commands.
Stopping for an upgrade requires its own runtime authorization; a downloaded
candidate is not permission to interrupt work.

1. Save `dradar fleet status --json`, `dradar leases`, `dradar update status
   --json` and `dradar status --json`. Identify the controller and exact batch
   IDs. Preserve local jobs, pending records, receipt identities, unknown exit
   evidence and the remaining lease deadline. A Fleet worker target is not
   proof of a running model or physical exit.
2. Use `dradar fleet claim-stop` to stop new claims in the selected HOME's
   claim window. In-flight claims remain saved; reconcile their original
   receipts with `dradar fleet claim-recover` when required. Never create a
   replacement allocation for an unknown receipt.
3. For each authorized ordinary Fleet batch, use `dradar fleet stop
   --batch-id <ID>` and `dradar fleet watch --batch-id <ID>`. A stop reply means
   the request was accepted, not that cleanup succeeded. Fleet interrupts an
   ordinary batch and retains its leases/results. A web run-plan batch instead
   stops that device and drains its current work under the existing run-plan
   contract; it can take longer. Check the pool's settled status, return code,
   exact process identity and durable session/container exit evidence. Unknown
   cleanup remains a blocker and is not erased by the controller's exit.
4. When no pools or active claim operation remain, the controller exits after
   its 30-second idle period. Verify the actual controller/launcher process
   exit and activity leases. A stalled claim or active pool must be resolved
   through its formal path; do not kill an unproven PID or remove locks.
5. The ordinary launcher activates a verified staged candidate only when all
   invocation leases and upload/runner safe-point blockers are clear. Use
   `dradar --version`, `dradar update status --json` and `dradar update doctor`
   to check the committed version/sequence and integrity. Staged is not
   committed. A pending completed result, including `server_secret_guard`,
   must continue blocking activation until an authorized exact result recovery
   resolves it. Removing pending records or forcing OTA pointers is not part
   of this route. A failed candidate self-test restores the last known good
   runtime and leaves the original work intact.
6. After the new runtime is committed, inspect the original batch and leases
   again. For an ordinary settled Fleet entry, `dradar fleet add --batch-id
   <ID> --benchmark <ORIGINAL> --workers <ORIGINAL> --retry` uses the exact
   batch's resume route. That route first processes pending uploads and only
   starts eligible still-held waiting work. It does not promise to rerun every
   leased/running row. Completed artifacts, pending uploads, unknown exits,
   expired leases, submitted work and changed ownership keep their existing
   safeguards. For a website run plan, use its original `dradar run --plan
   <CODE> --held-only --json` instructions; Fleet retry cannot reauthorize a
   stopped run-plan device. Do not re-enable refill or extend budgets merely
   to restore the original work.

Use a bounded maintenance window. If stop, exit proof, update activation or
resume is refused or times out, preserve the exact state and report the missing
evidence instead of repeatedly stopping processes or clearing blockers. This
route preserves work but does not guarantee that all blocker states can be
resolved within the window. It requires no new OAuth login.

Fleet claim process inspection uses Linux `/proc` argument boundaries and
PID start/parent identity. Quotes and newlines inside one Node/Codex task
argument are not shell syntax. Unreadable live identities, external DRadar
runners and unowned job containers still refuse admission; raw arguments are
not printed or saved by this check.
