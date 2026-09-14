# Optional managed-trial observations

This independent increment builds on the accepted phase-one/Windows-output candidate. No real source is automatically selected or imported. The trial capability is advertised only with a valid existing managed selection and a bounded read-only macOS arm64/local Unix Docker Linux-arm64 check. The check is repeated before a managed trial run; other providers retain their own paths.

The adapter creates an execution ID per consumer instance. Existing worker registration uses a constant client_seq=1 per process, so session identity alone cannot distinguish accidental duplicate consumers; the new execution ID supplements, rather than replaces, session/assignment/attempt/owner identity. Access-chain and generation tags are HMAC aliases scoped to the cohort and the host custody key. Keys and tokens never enter telemetry.

Refresh transaction start/end events are emitted only inside the actual host refresh gate callback; waiting/cached access is not reported as a second refresh. Delivery is distinct from native adoption. App-server running and native-child close timestamps are retained as bounded lifecycle metadata so fast turns cannot disappear between polls. Native acceptance is generation-specific and request use remains unknown. A new callback-delivered generation is not automatically labeled explicitly adopted.

The host runner validates a bounded private sidecar and forwards observations after its existing worker/owner handshake; no telemetry failure can grant a start permit or cause a model retry. Original source timestamps survive replay. Source emitted/dropped counters and server-recorded counts support coverage checks; absent final counters or lifecycle events remain unknown. Optional diagnostics are bounded and may be evicted before core events.

`auth_observed_v2` is separately negotiated and never mixed with core or v1-auth batches. Old/rolling server rejection leaves core worker registration usable. Unknown attributes and credentials/paths are rejected. Cross-device HMAC keys/clocks cannot be equated, and all events remain client-side observations rather than proof of absence of every duplicate process.

This code does not create an automatic gray rollout, monitor, external message channel or new telemetry platform. Initial roster/T0/tasks/window and any broader rollout require explicit operator decisions. Other OAuth providers have no new host-only refresh authority in this increment.
