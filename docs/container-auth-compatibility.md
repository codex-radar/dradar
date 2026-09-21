# Container authentication compatibility review

This is an implementation/validation matrix, not a claim of complete supported host renewal. Existing API-key paths remain static and never enter OAuth refresh. Radar login and scoring identity are unchanged.

| Runtime | Fixed version reviewed | Existing path | Host renewal / hot adoption |
|---|---|---|---|
| Codex / OpenAI | 0.154.0 (production resolves stable dynamically) | private native file copy | Real macOS ARM64 binary: offline initialize/account/read with empty and fake HOME verified. Managed admission API and durable staging tested, including four host processes with one fake OAuth rotation. Official refresh semaphore is process-local; unrelated-writer cooperation unverified. Stock exec does not consume the new DRadar AT file. Not activated. |
| Claude native OAuth | 2.1.251 | private native file copy | Real macOS ARM64 binary: offline version and empty auth status verified. Projection only; stable principal, native writer contract and actual renewal remain unverified. |
| Claude setup-token | 2.1.251 | process environment | Official docs specify the token is fixed for the session; replacement requires restart. No hot-reload promise or automatic model rerun. |
| Kimi | 0.39.1 | shared native directory | Exact tag source has provider lock/re-read; Windows explicitly skips that lock. Host-only renewal and cross-VM lock proof absent. |
| Grok | 1.0.40 | shared native directory | Public current docs/source describe an external auth command, but its compatibility with pinned 1.0.40 is unverified. No automatic host adapter activation. |
| Antigravity | 1.1.27 | shared native directory | Host-only renewal and token lifetime contracts unverified. New AT is not evidence of extended RT lifetime. |
| CodeBuddy | 2.137.1 | validated directory merge | Host-only renewal, principal discovery and hot adoption unverified. |
| ZCode | 0.16.5 | temporary API-key file | No OAuth refresh. |
| DeepSeek / Codex | stable version resolved per run | temporary API-key file | No OAuth refresh; includes main's catalog hotfix. |
| DeepSeek / DSH | 0.1.2-rc.1 | temporary API-key file | No OAuth refresh. |

The fixed review grid is Linux/macOS/Windows × x86_64/ARM64 × local/remote daemon for every row. Real Docker/account validation is **unknown for every cell** in this candidate. Pure Python/Pier fixture tests ran on macOS ARM64. Native macOS tests are outside Docker. Windows native containers are outside the existing Linux-image runtime.

Network Docker endpoints cannot use client-host shared OAuth directories. The new private file transport is topology-independent in its API, but each remote backend must still be validated. Windows durability and access controls are not certified by POSIX tests. Do not promote a source review, file delivery, or CLI account label to credential adoption or provider request success.

Sources (reviewed 2026-09-14):

- [Codex App Server](https://learn.chatgpt.com/docs/app-server), [0.154.0 auth manager](https://github.com/openai/codex/blob/6b9826e3aa83b1a5947db50f4332cb9c65f1b340/codex-rs/login/src/auth/manager.rs), [file storage](https://github.com/openai/codex/blob/6b9826e3aa83b1a5947db50f4332cb9c65f1b340/codex-rs/login/src/auth/storage.rs)
- [Claude environment contracts](https://code.claude.com/docs/en/env-vars)
- [Kimi 0.39.1 OAuth manager](https://github.com/MoonshotAI/kimi-code/blob/5efca0c3116743855c28426000073bfe34a4862f/packages/oauth/src/oauth-manager.ts)
- [Grok external authentication](https://docs.x.ai/build/enterprise)
- [Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/)


Controlled Codex admission currently allows only the verified macOS ARM64
0.154.0 binary. Its interactive provisioning step was mocked; no real account
was created or refreshed. The official native refresh used only a local fake
OAuth server, with other network destinations denied. Two uncoordinated native
app-server processes reproduced refresh-token reuse against that fake service;
this is a negative contract test, not an incident affecting a real account.
The supported host entry point is `ManagedAuthStore.session`, not a promise that
stock Pier's `codex exec` consumes or hot-reloads AT generations. That runtime
integration remains outside the current deliverable and the overall task is
not complete merely because the library and negative tests pass.
