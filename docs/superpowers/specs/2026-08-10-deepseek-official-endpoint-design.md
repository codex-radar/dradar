# DeepSeek Official Endpoint Runtime Snapshot

## Context

PR #73 made the DeepSeek endpoint locally selectable. That allows an existing
official DeepSeek credential to be sent to a third-party domain and lets a
local setting change the runtime of an assignment that the server authorized
only as the existing DeepSeek provider. The generated Codex configuration and
Pier network allowlist also resolve the setting at different times.

## Scope

This PR will remain client-only and official-only:

- Remove the `deepseek-endpoint` local configuration surface and all
  `opencode-go` runtime support.
- Keep the existing official DeepSeek credential, capability, catalog, runtime
  profile, and submission metadata semantics.
- Resolve the official provider URL once per command construction and use that
  same value for both Codex configuration and network policy.

OpenCode Go support is deferred until the server can authorize an endpoint and
preserve endpoint provenance through assignment, capability, submission, and
aggregation boundaries. That future work must also use endpoint-specific
credentials.

## Design

`providers.py` exposes one immutable official DeepSeek base URL. It no longer
reads `config.json` or contains a selectable endpoint table.

`runner.py` snapshots that URL while constructing a DeepSeek Pier command. The
snapshot is passed to:

1. the function that materializes the Codex TOML; and
2. the standalone Pier adapter through an agent constructor argument.

`pier_deepseek.py` never reads DRadar configuration. Its constructor validates
that the supplied URL is exactly the supported official URL, stores it, and
uses it to build the network allowlist. Any other URL fails before the task can
make a paid provider request.

The CLI and config display return to image-cache-only settings. Existing stale
`deepseek_endpoint` values in `config.json` are ignored rather than migrated or
acted upon.

## Failure Behavior

- A missing official credential continues to fail before Pier starts.
- A missing or modified model catalog continues to fail closed.
- A non-official adapter URL raises a configuration error before network use.
- No code path can select a third-party DeepSeek endpoint locally.

## Tests

Tests are added or updated in red-green order to prove:

- the Pier command gives the adapter the same official URL written to Codex
  TOML;
- the adapter rejects any non-official URL;
- the adapter allowlist uses the constructor-supplied official URL without
  reading `config.json`;
- the config CLI no longer accepts or displays a DeepSeek endpoint;
- existing official DeepSeek command, credential, catalog, and metadata tests
  remain green.

## Files

- `src/dradar/providers.py`
- `src/dradar/runner.py`
- `src/dradar/pier_deepseek.py`
- `src/dradar/image_cache.py`
- `src/dradar/cli.py`
- `tests/test_deepseek_provider.py`
- relevant CLI configuration tests
