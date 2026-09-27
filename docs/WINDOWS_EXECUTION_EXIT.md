# Windows execution and reservation exit evidence

Pier starts suspended, joins one exact Windows Job, verifies membership, then
resumes while the existing local launch/stop lock is held. There is no unbound
Popen fallback. The later provider permission still uses the same local intent
guard and current Server registration contract.

The Job has kill-on-close enabled and owns the host descendants of this attempt.
It does not own Docker's daemon or independent managed host groups. Normal exit
and cancellation still need a fresh exact-job Docker absence check. Missing Job
membership, failed process-count queries, unresolved descendants, failed handle
cleanup or unavailable container inventory retain unknown execution and capacity.

The local execution journal records a unique Job identity with the exact process,
execution, assignment, runner session, owner epoch and resume generation. Exit
evidence must match that original identity before a sealed session can request
capacity release. Logical close, wrapper return code and a sent kill request do
not authorize release. A failed close remains failed when a finalizer is repeated.

`test_windows_job_product.py` runs native no-model processes on Windows, including
parent exit with a live descendant, host exit, binding failures, query/handle
failures, unrelated-process isolation, and the audit-to-reservation path.
`test_windows_job_adapter.py` covers rejected evidence and adapter failures with
controlled substitutes. Skipped native tests on another OS are not Windows proof.

The native registration CI matrix is the isolation verification entry point.
It does not run real providers, Docker workloads or production claims. These
tests do not identify the cause of any older user's registration failure.
