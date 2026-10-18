# Original-input Pompeii images

`contexts/` is generated from the existing fixed16 task bundle by `prepare.py`.
The source bundle must match SHA-256 `f7bad307ac3127e63f17acf2cb59555a057e9afb616216365aa30217e0e733a3` and every selected input must match the sealed manifest in the Server repository. Only `environment/app/` is copied: original task imagery/schema/readme, the original empty answer seed, and the RP1 reference example. Target solutions, private tests/gold and credentials are excluded.

The base image is the existing immutable data-free runtime. Each image creates the original deterministic `pompeii-base` Git baseline. The workflow builds/pushes immutable per-task digests into the existing public package, then a fresh job with empty Docker credentials pulls every digest and verifies exact file hashes, UID/GID/cwd/HOME, Git baseline and unchanged fixed helper bind-auth/capture/pack without input mounts or network in the task container. No model, claim, result upload or grade is performed.

`source_identities.json` preserves original task identity. The build and anonymous acceptance receipts map it to actual new images; Server adoption is a separate reviewed change. Old images and claim replies stay intact.

The project owner confirmed existing material authorization on 2026-10-18; this statement records that confirmation and does not replace or rewrite the source license.
