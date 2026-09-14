# #0092 bounded native contracts: cli

Product commit: 3d4e2a868cfa31a39368d473e7a31ddcd8134821
Platforms: [ubuntu-latest, windows-latest]
Python: 3.12; uv: 0.9.7; dependencies: exact versions + universal SHA256 lock.
Selectors: `tests/test_version.py tests/test_auth_platform_limits.py tests/test_managed_auth_selection.py::test_default_status_does_not_create_managed_store tests/test_container_auth.py::test_existing_harness_contracts_and_no_secret_in_diagnostics tests/test_container_auth.py::test_wrong_source_type_fails_before_container_launch tests/test_packaged_adapter_resources.py::test_adapter_resources_are_materialized_without_network tests/test_packaged_adapter_resources.py::test_deepseek_constructor_uses_packaged_pin_and_rejects_tampering`
Additional probe: actual unsupported host, inactive managed commands with network/process/login traps, native default, version, materialized real Pier dependency import.
Per-job upper bound: 12 minutes; expected 5–10 minutes after queue. Existing macOS arm64 host/Linux arm64 Docker evidence reused; no repeat full QA or real credentials. Contents read only, no secrets/environment/deploy/signing; separate tools branch, immutable product checkout.

Incremental run: Windows probe only. Original 54 tests on each OS and Linux probe are reused. Retain real cp1252 stdout failure as a separate known defect; capture command text only to finish state/no-auth/dependency contract. Product SHA unchanged.

Encoding fix native regression: product 2d95655b492303124fad4d07bcc6a95cb67f341a; Linux+Windows only two real redirected cp1252/UTF8 tests plus original UNCAPTURED native probe. Reuse previous54 each. No product-wide stdout reconfiguration; original failing run retained.
