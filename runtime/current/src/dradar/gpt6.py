"""GPT-6 Sol/Luna and GPT-6.1 Sol Codex benchmark contracts.

Ultra is Codex orchestration, not a Responses API reasoning effort.
Runtime pins are DRadar candidates; upstream minimum and subscription access
remain unverified until an authorized Codex Harness run succeeds.
"""
GPT6_EFFORTS = {
    "gpt-6-sol": ("low", "medium", "high", "xhigh", "max", "ultra"),
    "gpt-6-luna": ("low", "medium", "high", "xhigh", "max"),
    "gpt-6.1-sol": ("low", "medium", "high", "xhigh", "max"),
}
GPT6_CAPABILITY = "codex-gpt6-sol-luna-v1"
GPT6_CODEX_VERSION = "0.155.1"
GPT61_CAPABILITY = "codex-gpt6-1-sol-v1"
GPT61_CODEX_VERSION = "0.159.2"
GPT6_MODEL_VERSIONS = {model: GPT6_CODEX_VERSION for model in ("gpt-6-sol", "gpt-6-luna")}
GPT6_MODEL_VERSIONS["gpt-6.1-sol"] = GPT61_CODEX_VERSION
GPT6_MODEL_CAPABILITIES = {model: GPT6_CAPABILITY for model in ("gpt-6-sol", "gpt-6-luna")}
GPT6_MODEL_CAPABILITIES["gpt-6.1-sol"] = GPT61_CAPABILITY
CACHE_WRITE_MODELS = frozenset({"gpt-6-astra", *GPT6_EFFORTS})
