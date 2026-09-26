# Recover saved results without running the model again

An upload failure does not mean the model needs to answer the task again.
DRadar preserves the original files and a pending record. Saved results,
cleanup records, and historical results whose pending entry is missing block
another model run for the same assignment.

## Network or service failure

For an ordinary account, run `dradar retry-upload`. For a web run plan, use
`dradar run --plan YOUR_RUN_CODE --upload-only --json`. These commands upload
existing results; they do not claim another task or run the model. Keep the
same server, account, benchmark and plan. A changed credential does not grant
permission to upload records belonging to another scope.

## Upload blocked by the credential scanner

The message names the scanner rule and preserved artifact path. DRadar cannot
safely redact some patch context or removed lines without changing the patch.
It keeps an `upload_blocked` record and does not retry or run the task again
automatically. Keep the original files and record the assignment ID and rule
name for review. Do not paste the raw patch or credentials into a report. An
updated scanner or an explicit reviewed recovery must still pass the safety
checks; deleting pending records or bypassing scanning is not a recovery.

## Files exist but no pending record can be found

Keep the job directory. `dradar retry-upload` reports this condition instead
of claiming that every result was uploaded. Use the original web plan's
`dradar progress --plan YOUR_RUN_CODE --json` to inspect its state. The result
must be matched to the exact assignment and its accepted submission before
an upload record can be restored. A directory name alone does not establish
upload ownership. When that metadata cannot be verified, DRadar preserves the
files and refuses another model run; report the assignment ID and missing
metadata for review.

## Expired assignment or permanent server rejection

The result remains local, with the rejection reason. Retrying the same payload
does not remove the block. Expiry is not authority to delete paid work. An
`owner_superseded` result may use the existing explicit salvage command; other
blocks cannot be cleared by that command.

## Damaged local ledger or unconfirmed cleanup

DRadar leaves the original file unchanged and stops new work. Preserve the
file before investigating filesystem access or restoring a verified backup.
Do not replace an unreadable ledger with an empty one. A cleanup quarantine
requires evidence that the exact managed process tree and containers exited;
elapsed time or a missing parent PID is insufficient.

The official `dradar stop --plan YOUR_RUN_CODE --scope this-device --json`
command remains the way to request a device stop. Stopping does not remove
saved results or establish that cleanup has completed.
