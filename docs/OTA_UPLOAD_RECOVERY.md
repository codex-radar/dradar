# Signed upload-only recovery while OTA waits for pending work

Normal OTA activation still requires `runloop_safe_point()` with zero pending
uploads. A completed result can therefore prevent activation of the version
that knows how to retry that result. The `recover-upload` entry in a signed OTA
zipapp handles **one existing assignment** without changing the active CLI,
claiming work, starting a model, or requesting a new upload owner.

## Trust chain and operator sequence

1. Keep the real `DRADAR_HOME` unchanged. Record its `dradar update status
   --json`, the exact pending assignment, saved benchmark, batch and runner
   session, and the server/account identity. Stop any runner through its normal
   control flow. A current `waiting_safe_point` state is expected; never fake a
   safe point or edit the pending ledger or OTA pointers.
2. Pin the official stable signed manifest and its exact platform package for
   the recovery version. Use the already trusted, installed CLI (for example
   0.5.229) with its **public** `update prepare --manifest SIGNED_JSON
   --trusted-key KEY_ID=RAW_PUBLIC_KEY` in a fresh, disposable physical
   `DRADAR_HOME`. The public key must be the production key already embedded in
   that trusted CLI, not one supplied by the manifest. The old CLI performs
   signature, rollout, compatibility, size and SHA-256 checks before running
   the candidate. Its discovery may also install the same signed version in
   this disposable home. Inspect `update status --json` and the release record
   there; require the pinned version, sequence, target and package digest.
   **Do not run `update prepare` in the real home:** an older candidate is
   already staged there. All scratch OTA mutations stay in the disposable
   home, which contains no real pending result.
3. Run the verified package from that disposable home's
   `ota/releases/RELEASE_ID/PLATFORM.pyz` path, with `DRADAR_HOME` set to the
   original real home:

   ```sh
   python /absolute/path/to/verified-candidate.pyz recover-upload \
     --manifest /absolute/path/to/pinned-signed-manifest.json \
     --assignment-id 0123456789abcdef0123456789abcdef \
     --benchmark deep-swe \
     --batch-id 550e8400e29b41d4a716446655440000 \
     --runner-session-id SAVED_SESSION_ID
   ```

   Omit `--batch-id` only for a saved personal row without a batch. The
   session argument is optional for old rows lacking one; when supplied it
   must match the saved row. The candidate rechecks the production signature
   and its exact package digest, real signed committed pointer, version and
   anti-rollback policy, then refuses an active runner or competing recovery.
   Its upload path uses the existing server/account/benchmark/batch scope and
   the original server upload-intent, session, lease and owner fences.
4. Treat any owner conflict, expired lease, identity mismatch, network error,
   or blocked row as a stop. The command never asks the server for a new owner.
   Inspect the server submission and saved ledger through normal read-only
   tools before deciding whether any later retry is appropriate. Once the
   ledger is empty, the ordinary launcher can activate its staged update at a
   genuine safe point.

`recover-upload --help` is available on the candidate zipapp without touching
the real home. Running an unverified downloaded zipapp is not the trust
bootstrap: complete step 2 with an already trusted CLI first. A signed
manifest alone also does not override a server owner or lease rejection.
