# #0132 — GPT-6 Sol and Luna staged integration

This is a development candidate, not a release approval or proof of real model availability.

## Contract and sources (checked 2026-09-23)

- Exact IDs: `gpt-6-sol`, `gpt-6-luna`. No alias to GPT-5.6 and no historical repricing.
- Benchmark effort lanes: Sol low/medium/high/xhigh/max/ultra; Luna low/medium/high/xhigh/max. Ultra is Codex orchestration, not an API reasoning effort. API `none` is not a benchmark lane in this rollout.
- Standard API-equivalent $/million tokens, input/cache-read/cache-write/output: Sol short 2/.2/2.5/10, long 4/.4/5/15; Luna short .1/.01/.125/.5, long .2/.02/.25/.75. Long context applies per request only above 272000 input tokens.
- Cache-write tokens must be independently derived from raw events. Missing evidence yields unknown cost, never zero or a borrowed older tariff. Subscription payments and Fast/Batch/Flex spend are not represented by this Standard comparison.
- Sources: https://learn.chatgpt.com/docs/models ; https://developers.openai.com/api/docs/models/gpt-6-sol ; https://developers.openai.com/api/docs/models/gpt-6-luna ; https://developers.openai.com/api/docs/pricing .

## Runtime

The stable npm/GitHub release inspected is Codex 0.155.1, used as this candidate's runtime floor, NOT a proven upstream minimum. Ordinary Pier 0.3.0 already installs an exact resolved npm version in the container layer, invalidating cached older versions. New clients reject older versions for these new models. The DRadar OTA is a Python client artifact; it does not contain the Codex binary. Codex is installed in the benchmark runtime separately. Host login/doctor installers are separate from that runtime.

The official darwin-arm64 0.155.1 package passed npm SHA512 integrity and native `--version`; generated app-server schema was inspected without a model call. SHA256 of native executable: 8eaf1ad12fe6bf89b1710330f58900014322c7c5af677e43be116d8ac5fc0a9e. Six-platform npm distributions exist; execution of all six binaries and six-platform OTA build verification remain release gates.

The experimental managed-auth host credential authority remains pinned to its reviewed 0.154.0 native binary and unchanged custody/refresh contract. For GPT-6 Sol/Luna, new managed assignments pin their separate task container to Codex 0.155.1; older managed assignments retain 0.154.0. The server advertises and checks the model-specific container version and GPT-6 client capability before leasing and on continuation. The client checks the exact descriptor and container output. The official 0.155.1 app-server accepted a synthetic external ChatGPT token in an isolated temporary home without a provider request; fake-token success verifies protocol shape, not real account/model adoption or refresh behavior. The managed mode remains a bounded experimental cohort. Existing stores, users and in-flight work have not been modified.

## Staged rollout and compatibility

New configurations start `paused: true`; operator activation is a separate reviewed config change after real validation. Server capability `codex-gpt6-sol-luna-v1` gates new-model clients; old-model behavior remains. Both DeepSWE and Pompeii config generators include separate new identities. Frontend filters, labels, cards, colors, report ordering and paused state cover both generations.

Server config in production is carried from the live release tree, not automatically replaced with this repository config. Deployment must apply an additive, reviewed live-config patch to BOTH benchmark config lists and load the correct tariff into the live price matrix; do not replace a live config with this example or copy production user data.

Release sequence proposed: fixed-candidate independent QA and real four-case validation, then Server code/config/tariff, Web, CLI OTA, and finally reviewed activation. Recheck latest bases and #0131 overlap before serial merge/release; old clients remain gated until upgraded. Rollback starts by pausing only the new model lanes, then restore each recorded prior artifact/config. Never delete new-model history to roll back.

## Validation boundaries

Offline tests exercise real production parsers, request construction, assignment/upload endpoints and public cost consumption with synthetic local fixtures. Grade updates in the upload test are explicitly fixture mutations; they do NOT prove real grader or real model execution. Native `--version` and schema output are not a real trial.

D0132-REAL-04 supersedes the earlier hard-dollar precondition: $5 per task and $20 total are observed stop thresholds, not guaranteed maximum charges. The site owner later confirmed that Sol and Luna can now be called; the earlier unavailable-model report is historical. The authorized trial is limited to one isolated ds0 subscription runner, four Sol/Luna × DeepSWE/Pompeii attempts at medium effort, one at a time, without refill or replay. Before the first attempt, the operator must verify the actual container version, isolated server and account route, stop control, core quota, and that the runner has no API-key or paid extra-usage fallback. No trial, real grading, returned-model identity, complete actual usage, or production release has yet been verified.
