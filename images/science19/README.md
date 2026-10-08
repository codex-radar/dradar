# Science19 Agent environments

The fixed DRadar selection is task046–064, not the full upstream library. These
images are built from pinned official Science `environment/` Dockerfiles and
their original contexts. Upstream does not declare prebuilt task images; these
builds are not claimed as unchanged mirrors of an upstream prebuilt image.

Only Agent environments are published here. Original `tests/` verifier contexts,
solutions and authoring materials are excluded. The Server must run the original
separate no-network verifier and preserve its task-specific privilege transitions.
No model, answer generation, grading, account or production operation occurs in
this image workflow. Legal zero results and previous execution evidence are not
changed. The exact resulting image digests are bound only after successful source
verification, CI build, public package readback and full anonymous fresh-state pull.

Each fresh source-build tag includes the original source commit, original task
content hash, reviewed CI commit, run ID and attempt. Existing versions are not replaced. The
source Dockerfiles and contexts stay byte exact; two explicit OCI metadata labels
associate these source builds with the publishing repository and reviewed commit.
The fixed source's Apache-2.0 LICENSE is preserved in each build receipt; upstream
data descriptions and third-party conditions remain authoritative.
