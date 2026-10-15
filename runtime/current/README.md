# Current installed CLI source

## Current release target

Build `runtime/current` for CLI **0.5.300** / Server **0.1.2.post83**.
The paired target is `RELEASE_CORE_CHAIN.json`, identified by
`SOURCE_MANIFEST.json.current_release_file`. Its schema and values are identical
in both repositories. `RELEASE_PAIR.json` and the manifest `release_pair` field
are retained historical receipts, not current version selectors. Deployment
status is established by the installed runtime and production readback.

## Historical source and release records

This directory records the installed single-task CLI used by the current server
environment. Every package file was matched byte-for-byte to a local source file
and the installed-file SHA-256 inventory on 2026-10-13. The independent and native
experimental candidates are intentionally excluded. Package version remains
0.5.298+single.2; the overall project source release is 2026.10.13.

Build this package from this directory with the existing Hatchling build backend.
Environment-specific credentials and benchmark results are external inputs and
are not included. This source synchronization does not install an OTA, launch a
model, resume a paused task, or change production.

Paired recovery release: CLI 0.5.299 / Server 0.1.2.post74. Build this directory, not historical root src. See RELEASE_PAIR.json. Release does not resume paused runs.
