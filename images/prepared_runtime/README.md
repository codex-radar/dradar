# Prepared public task images

These images supply dependencies for the public **Agent + Skill + fixed helper**
flow. They do not install or invoke DRadar CLI. Model tools remain separate.

`BATCH.json` is a fixed dependency audit: 64 official tasks use 49 Agent images.
Seventeen complete images are reused; only the 32 measured gaps are rebuilt.
The immutable source image is the first filesystem layer chain. Original cwd,
HOME, UID/GID, command and entrypoint are checked; the nonroot task stays nonroot.
Verifier images, task statements, hashes, answers and accounts are not changed.

Missing Node/npm comes from the Linux amd64 manifest of official Node 22.23.3,
`docker.io/library/node@sha256:efd0ab5780c2d9ab1f0f869571a00d5edb17793bff4cce4a2792e3eb0ffc7562`.
React keeps its existing Node/npm. React and Duan install Python and system roots
with the image's signed official package manager, without upgrading installed
packages. Build receipts retain the installed package versions and config.
The Duan base has a packaged root bundle but lacks OpenSSL's default cert.pem
link; its dependency layer restores that link to the same system bundle. The
nonroot risk-scorer base's project is not writable by its declared execution
identity; only that project's ownership is prepared for UID/GID 65534, with its
file bytes and modes independently compared before and after the build.

The explicit workflow is confined to the reviewed branch, actor, repository,
batch SHA256 and full source commit, with two parallel jobs and the existing
`ghcr-environment-publication` concurrency group. Build tags include the complete
source commit; delivery uses registry digests. Existing tags/digests are retained.
No task, model, account or production lease is acquired by this code.

The `accept` phase uses a separate runner with an empty Docker authentication
configuration and pulls the actual published digest. The offline probe checks
the declared identity, Python, default roots, Node/npm, shell, writable project,
HOME and temp directories, plus absence of a `dradar` executable. It is dependency
verification, not a real-model or official-grader end-to-end result. Public helper
verification, strict HTTPS and the model-tool startup boundary have their own
fixed outer-Agent acceptance evidence.

Publishing an image does not activate it. The current formal identity includes
the original image digest. Backend integration must explicitly preserve prior
issued identities and original helper contracts; this directory never rewrites
the live catalog, task hashes, scores or in-flight runs.
