# Artifact boundary refusal: preserve, review, revalidate

`unsafe_artifact` is a local safety block, not a transient upload error. The
fixed `artifact_boundary_reason` code identifies the rejected condition without
printing filenames, log contents, credentials, or account information.

`retry-upload` preserves this block. `salvage` supports `owner_superseded` only;
it is not a recovery command for unsafe input. Do not edit the pending ledger
to remove the flag, rename a rejected file into an accepted location, or downgrade
the client. There is currently **no supported self-service unblock command**.

## Operator workflow

1. Stop attempts to upload this assignment. Keep the original trial, pending
   record, and failure code. Do not delete or modify the rejected materials.
   Ask the station operator for a security review, providing only the assignment
   reference, client/platform versions and fixed reason code. Do not send raw
   logs, files, credentials, or private paths to public channels.
2. An explicitly authorized reviewer checks the source and host boundary using
   read-only access. Distinguish unsupported platform, missing/invalid host
   output, file-type/path/identity refusal and size limits. A renamed file or a
   successful secret scrub is not evidence that its source is safe.
3. If local evidence must be preserved elsewhere, an authorized operator prepares
   a traceable, access-controlled copy using a safe evidence collection process;
   this document does not authorize reading a rejected target or following links.
   Preserve the original and record permitted metadata/digests only.
4. A candidate repair must first be revalidated **offline** in an isolated copy,
   on a supported native platform. Revalidate the complete source boundary,
   file types and identities, size limits, post-run output contract, assignment
   binding, patch digest, and the single-snapshot usage/attachment contract.
   Use a mock API that cannot contact the real upload/grading service. Both
   normal content and the original refusal condition need review. Passing a
   format parser alone is insufficient.
5. The current client intentionally cannot turn this review into an automatic
   retry. Return the evidence to the station operator. Any actual recovery needs
   a separately reviewed and authorized recovery operation that revalidates at
   the moment of use, preserves original evidence, respects assignment/nonce and
   ownership rules, and receives explicit upload authorization. Until such an
   operation is available and approved, leave the original assignment blocked.
   Do not simply clear the flag after an offline test, and do not automatically
   delete evidence or restart a paid task.

## Post-run diagnostics

- `required_trajectory_missing`: nonempty conversion input existed, but this
  invocation produced no required trajectory. Upstream may have logged and
  swallowed a conversion error. The host state remains incomplete.
- `invalid_post_run_output`: a generated output failed the expected JSON/trajectory
  contract. The host state remains incomplete.
- `post_run_not_finalized`: the consumer refuses an incomplete host generation;
  it must not reuse an earlier output.
- `no_conversion_input`: successful intentional absence when there was no
  nonempty source session/stream; this is distinct from conversion failure.
- `platform_boundary_unavailable`: required safe platform primitives are absent.
  The candidate checks this before `go` can claim work and before direct
  `run_trial` starts an agent. This guard reduces wasted quota; it does **not**
  restore native Windows support or approve a reduced platform release.

Native Windows uses held Win32 handles, opened-object reparse/type/identity checks,
read/write sharing restrictions, and ACL owner validation. This backend is limited
to fixed local NTFS volumes; mapped network drives and other filesystems fail
closed. OWNER RIGHTS is accepted only after verifying that the object owner is
the current user. Other writable principals remain rejected. Linux uid/gid values
are not invented for Windows ACL ownership.

Release still requires independent native platform verification. WSL and Linux
containers cannot supply that evidence. Existing blocked assignments remain
preserved and cannot be unblocked simply because a newer backend is available.


## POSIX trial creation contract

New runs launch Pier with a child-process-only umask of 077. Unmodified upstream
TrialPaths.mkdir therefore creates new trial roots as 0700 even when the calling
shell uses 002. The CLI does not change its own/global umask, and does not chmod
existing trials. Existing group/world-writable roots still lack the required
private-host precondition: retain their evidence and use the authorized offline
review workflow above. This is not an automatic migration or unblock operation.
