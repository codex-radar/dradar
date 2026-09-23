"""GPT-6 Sol/Luna Codex benchmark contract (official docs, 2026-09-23).

Ultra is Codex orchestration, not a Responses API reasoning effort.
0.155.1 is our candidate runtime floor; upstream minimum remains unverified.
"""
GPT6_EFFORTS = {
    "gpt-6-sol": ("low", "medium", "high", "xhigh", "max", "ultra"),
    "gpt-6-luna": ("low", "medium", "high", "xhigh", "max"),
}
GPT6_CAPABILITY = "codex-gpt6-sol-luna-v1"
GPT6_CODEX_VERSION = "0.155.1"
CACHE_WRITE_MODELS = frozenset({"gpt-6-astra", *GPT6_EFFORTS})
