# Targeted runtime recovery checks

Run from the repository root with the current runtime source, not historical root src:

```
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=runtime/current/src python -m pytest -q runtime/current/tests/test_claim_retry_recovery.py
```

Requires pytest and the runtime httpx dependency. All transport is httpx.MockTransport; no network, model or production state. Covers retry restart, lost ACK, existing cached retry recovery without ID rotation, retained first retry, definitive receipt immutability and unchanged request fingerprint.

The recovery contract requires the matching server fix. Nonterminal single-task claim retries may be observed again with the same exact request; successful allocations stay immutable. This is not authorization to restart paused runs.
