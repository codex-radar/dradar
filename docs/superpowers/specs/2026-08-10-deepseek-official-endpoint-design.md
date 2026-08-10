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

## Addendum: OpenCode Go via server-authorized assignment (2026-08-10+)

The OpenCode Go gateway (`https://opencode.ai/zen/go/v1`) is reintroduced
client-side only under the conditions the reviewer set for the original
switchable-endpoint PR:

- **No local endpoint switch.** The endpoint is chosen by the assignment's
  `provider` value (`deepseek` or `opencode-go`), which only the server can
  issue. `providers.py` holds immutable URL constants; the CLI/config surface
  gains no endpoint setting.
- **Per-endpoint credentials.** OpenCode Go reads only
  `~/.dradar/secrets/opencode_api_key` or `OPENCODE_API_KEY`
  (`dradar provider setup opencode-go`). `create_provider_auth_json()` fails
  closed when the run's own credential is missing; tests prove the official
  key never enters an OpenCode Go auth.json and vice versa.
- **Distinct provenance.** OpenCode Go advertises its own capability
  (`codex-deepseek-v4-flash-opencode-go-v1`) and submits its own
  `DEEPSEEK_OPENCODE_RUN_CONFIG_VERSION`/`DEEPSEEK_OPENCODE_RUNTIME_PROFILE`
  plus an explicit `model_endpoint` field. Official submissions keep their
  byte-identical metadata shape.
- **One resolved URL per command.** The runner snapshots the provider URL
  once and feeds the same value to the Codex TOML and the adapter
  constructor; the adapter accepts exactly the two authorized URLs and
  builds the allowlist from the constructor value only.
- **Ambient redirect residue closed.** The DeepSeek-family pier environment
  now also strips `OPENAI_BASE_URL`/`OPENAI_API_BASE` so stock Pier's Codex
  agent cannot append a third-party `openai_base_url` to the container
  config.

Server-side authorization remains the gate: a server that does not emit
`provider = "opencode-go"` assignments or accept the new capability cannot
route anyone to the gateway, and a client without the gateway credential
refuses the run before Pier starts.

### Live verification (2026-08-10, trial key)

Probed from a host outside the task container with a trial key stored in the
pi agent auth file (`opencode-go.key`, type `api_key`):

- `GET https://opencode.ai/zen/go/v1/models` with the key -> 200, 25 models,
  including exactly `deepseek-v4-flash` (matches the bundled catalog slug).
- `POST https://opencode.ai/zen/go/v1/responses` (`deepseek-v4-flash`,
  `max_output_tokens: 1`) -> 200 in ~20s; usage reported OpenAI-compatible
  (`input_tokens`/`output_tokens`/`reasoning_tokens`), final text empty
  because the single budgeted token was consumed by reasoning - expected for
  this reasoning model, and the response envelope matches Codex's
  `wire_api = "responses"` contract.
- The gateway sits behind Cloudflare: requests without a `User-Agent` are
  blocked (403, CF error 1010/1015-style); `codex/0.146.0`, `node`, `curl`
  and browser UAs all pass. Codex CLI's own UA is accepted, but egress from
  the task container's IP was not directly exercised - a real assignment is
  the remaining end-to-end check.
- `GET https://api.deepseek.com/` (unauthenticated) -> 401, official route
  alive.
