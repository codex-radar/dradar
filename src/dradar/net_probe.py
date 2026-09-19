"""Bounded DNS + TLS reachability probes for the registries Pier needs.

A volunteer whose resolver cannot answer for ``ghcr.io`` does not see a DNS
error.  Docker retries inside its own client, Pier keeps waiting for the
image, and the CLI shows nothing at all until the preparation grace window
expires (volunteer report, 2026-09-08: WSL2's NAT resolver could not answer
for ``ghcr.io`` and ``docker.io`` TLS never completed).  ``dradar doctor``
never touched those hosts, so the one command a volunteer runs to find out
whether their machine is ready stayed silent about the thing that was
broken.

These probes run in the CLI's own process, so they report *which* host is
unreachable and *whether* it failed at name resolution or at the TLS
handshake.  The two need different fixes -- a resolver override versus a
proxy/mirror -- and the volunteer cannot tell them apart from Docker's
output.

Every phase is bounded.  ``socket.getaddrinfo`` cannot be interrupted once
it has entered the platform resolver, so it runs on a daemon thread that
the caller abandons when the budget expires.  The probe returns a verdict
either way: a diagnostic that can hang is worse than no diagnostic,
because the volunteer assumes it is still checking.
"""

from __future__ import annotations

import os
import socket
import ssl
import threading
import time
from dataclasses import dataclass

import certifi

# A healthy resolver answers in well under a second; five seconds still
# covers a cold cache behind a VPN.  Eight seconds for connect+handshake
# covers a slow proxy without letting one dead host dominate the report:
# two hosts cost at most 26 s, against the 6 min of silence measured on the
# current build path (#0152).
DNS_TIMEOUT_SEC = 5.0
TLS_TIMEOUT_SEC = 8.0

# ``ghcr.io`` serves the pinned Pier egress proxy image; ``auth.docker.io``
# issues the anonymous token every Docker Hub base-image pull needs.  Either
# one being unreachable stalls an environment build, and neither was covered
# by the existing Docker Hub preflight, which only exercised
# ``docker.io/library/ubuntu``.
REGISTRY_PROBE_HOSTS = ("ghcr.io", "auth.docker.io")

_PROXY_ENV_VARS = (
    "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy",
)

# clash/mihomo-style proxies answer every query from a synthetic pool instead
# of the real resolver, so a name that does not exist still "resolves".  On
# such a machine a DNS probe can only ever say yes, and the TLS handshake is
# the first phase that carries real information.  Measured on the maintainer's
# own Mac (#0152): ``ghcr.io.invalid-dradar-0152`` resolved to 198.18.0.114.
_FAKE_IP_PREFIXES = ("198.18.", "198.19.", "fdfe:dcba:9876:")


def proxy_configured(env: dict[str, str] | None = None) -> str | None:
    """Return the name of the first proxy variable set in ``env``.

    A configured proxy makes a direct probe inconclusive: the proxy, not
    this process, resolves the registry name, and Docker may additionally
    carry a daemon-side proxy this process cannot see.  Callers downgrade a
    failure to a warning rather than telling a volunteer their working
    machine is broken.
    """

    source = os.environ if env is None else env
    for name in _PROXY_ENV_VARS:
        if (source.get(name) or "").strip():
            return name
    return None


@dataclass(frozen=True)
class HostProbe:
    """One registry host's reachability, split by the phase that failed."""

    host: str
    stage: str  # "ok" | "dns" | "tls"
    dns_ms: float | None = None
    tls_ms: float | None = None
    detail: str = ""
    synthetic_dns: bool = False

    @property
    def ok(self) -> bool:
        return self.stage == "ok"


def _resolve(host: str, timeout: float) -> tuple[list[str] | None, float, str]:
    """Resolve ``host`` on an abandonable thread, bounded by ``timeout``.

    The thread is a daemon and is deliberately not joined on timeout: the
    platform resolver owns it and there is no portable way to cancel it.
    Abandoning one short-lived thread per unreachable host is the price of
    keeping the probe itself bounded.
    """

    result: dict[str, object] = {}

    def run() -> None:
        try:
            infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
            result["addrs"] = sorted({info[4][0] for info in infos})
        except OSError as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"

    worker = threading.Thread(
        target=run, name=f"dradar-dns-{host}", daemon=True,
    )
    started = time.monotonic()
    worker.start()
    worker.join(timeout)
    elapsed_ms = (time.monotonic() - started) * 1000.0
    if worker.is_alive():
        return None, elapsed_ms, f"no answer within {timeout:g}s"
    addrs = result.get("addrs")
    if isinstance(addrs, list) and addrs:
        return addrs, elapsed_ms, ""
    error = result.get("error")
    return None, elapsed_ms, str(error) if error else "resolver returned no address"


def _tls_handshake(host: str, timeout: float) -> tuple[bool, float, str]:
    """Complete a real TLS handshake to ``host``:443 within ``timeout``."""

    context = ssl.create_default_context(cafile=certifi.where())
    started = time.monotonic()
    try:
        with socket.create_connection((host, 443), timeout=timeout) as raw:
            raw.settimeout(timeout)
            with context.wrap_socket(raw, server_hostname=host):
                pass
    except (OSError, ssl.SSLError) as exc:
        return False, (time.monotonic() - started) * 1000.0, f"{type(exc).__name__}: {exc}"
    return True, (time.monotonic() - started) * 1000.0, ""


def probe_host(
    host: str,
    *,
    dns_timeout: float = DNS_TIMEOUT_SEC,
    tls_timeout: float = TLS_TIMEOUT_SEC,
) -> HostProbe:
    """Resolve ``host`` and complete a TLS handshake, each within budget."""

    addrs, dns_ms, dns_error = _resolve(host, dns_timeout)
    if addrs is None:
        return HostProbe(host, "dns", dns_ms=dns_ms, detail=dns_error)
    synthetic = any(
        addr.startswith(prefix)
        for addr in addrs
        for prefix in _FAKE_IP_PREFIXES
    )
    tls_ok, tls_ms, tls_error = _tls_handshake(host, tls_timeout)
    if not tls_ok:
        return HostProbe(
            host, "tls", dns_ms=dns_ms, tls_ms=tls_ms, detail=tls_error,
            synthetic_dns=synthetic,
        )
    return HostProbe(
        host, "ok", dns_ms=dns_ms, tls_ms=tls_ms, synthetic_dns=synthetic,
    )


def probe_registries(
    hosts: tuple[str, ...] = REGISTRY_PROBE_HOSTS,
    *,
    dns_timeout: float = DNS_TIMEOUT_SEC,
    tls_timeout: float = TLS_TIMEOUT_SEC,
) -> list[HostProbe]:
    """Probe every registry host Pier needs, in order, each one bounded."""

    return [
        probe_host(host, dns_timeout=dns_timeout, tls_timeout=tls_timeout)
        for host in hosts
    ]


def _resolver_hint(platform: str) -> str:
    if platform == "wsl":
        return (
            "WSL2's NAT resolver often cannot answer for container registries; "
            "pin a public resolver in /etc/resolv.conf and set "
            "generateResolvConf=false in /etc/wsl.conf, then `wsl --shutdown`"
        )
    if platform == "windows":
        return (
            "set a working DNS server on the active network adapter, then "
            "restart Docker Desktop"
        )
    if platform == "macos":
        return (
            "set a working DNS server in System Settings > Network, then "
            "restart OrbStack/Docker Desktop"
        )
    return "point /etc/resolv.conf at a resolver that can answer, then restart Docker"


def probe_advice(probe: HostProbe, platform: str) -> str:
    """The fix for one failed probe, without naming the host.

    Kept separate from :func:`probe_hint` so a report covering several hosts
    that failed the same way can print the recipe once instead of burying it
    under a repeat per host.
    """

    if probe.stage == "dns":
        return _resolver_hint(platform)
    if probe.stage != "tls":
        return ""
    lowered = probe.detail.lower()
    if (
        "certificate verify failed" in lowered
        or "sslcertverificationerror" in lowered
        or "hostname mismatch" in lowered
        or "self-signed certificate" in lowered
    ):
        return (
            "a proxy or filter is intercepting the connection — trust its CA "
            "or exclude the registry from interception"
        )
    return (
        "allow outbound 443 to it, or configure a registry mirror/proxy "
        "Docker can reach"
    )


def failure_hint(probes: list[HostProbe], platform: str) -> str:
    """One actionable line for every host that failed, advice not repeated."""

    failed = [probe for probe in probes if not probe.ok]
    if not failed:
        return ""
    hosts = ", ".join(probe.host for probe in failed)
    stage = "did not resolve" if failed[0].stage == "dns" else "did not finish TLS"
    detail = failed[0].detail
    advice = "; ".join(
        dict.fromkeys(probe_advice(probe, platform) for probe in failed)
    )
    return f"{hosts} {stage} ({detail}); {advice}"


def probe_hint(probe: HostProbe, platform: str) -> str:
    """Turn one failed probe into the fix that addresses *that* phase."""

    if probe.stage == "dns":
        return (
            f"{probe.host} did not resolve ({probe.detail}); "
            + _resolver_hint(platform)
        )
    if probe.stage == "tls":
        lowered = probe.detail.lower()
        # Only a verification failure proves interception.  A generic
        # SSLEOFError/timeout means the connection died before any
        # certificate was validated, and calling that "unexpected
        # certificate" sends the volunteer after the wrong fix.
        if (
            "certificate verify failed" in lowered
            or "sslcertverificationerror" in lowered
            or "hostname mismatch" in lowered
            or "self-signed certificate" in lowered
        ):
            return (
                f"{probe.host} resolved but its TLS certificate did not "
                f"verify ({probe.detail}); a proxy or filter is intercepting "
                "the connection — trust its CA or exclude the registry from "
                "interception"
            )
        resolved = (
            "resolved through a proxy's synthetic DNS pool, so the address "
            "proves nothing, and the connection"
            if probe.synthetic_dns else
            "resolved but the TLS handshake"
        )
        return (
            f"{probe.host} {resolved} did not complete ({probe.detail}); "
            "allow outbound 443 to it, or configure a registry mirror/proxy "
            "Docker can reach"
        )
    return ""


def summarize(probes: list[HostProbe]) -> str:
    """One line per host, timings included, for the doctor report."""

    parts = []
    for probe in probes:
        if probe.ok:
            dns = "dns via proxy" if probe.synthetic_dns else f"dns {probe.dns_ms:.0f}ms"
            parts.append(f"{probe.host} ok ({dns}, tls {probe.tls_ms:.0f}ms)")
        elif probe.stage == "dns":
            parts.append(f"{probe.host} DNS failed")
        else:
            parts.append(f"{probe.host} TLS failed")
    return "; ".join(parts)


# Markers Docker/BuildKit and Pier print when a registry cannot be reached.
# Matched against the build log so a stalled or failed environment build can
# name the phase instead of returning a bare "runner failed".
_DNS_MARKERS = (
    "no such host",
    "server misbehaving",
    "temporary failure in name resolution",
    "name resolution",
    "could not resolve",
    "lookup ",
)
_TLS_MARKERS = (
    "tls handshake timeout",
    "tls: handshake failure",
    "x509:",
    "certificate is valid for",
    "certificate signed by unknown authority",
)
_CONNECT_MARKERS = (
    "i/o timeout",
    "connection refused",
    "network is unreachable",
    "no route to host",
    "context deadline exceeded",
    "dial tcp",
)
_AUTH_MARKERS = (
    "failed to fetch anonymous token",
    "unauthorized: authentication required",
    "toomanyrequests",
    "429 too many requests",
)

_REGISTRY_HOSTS_IN_LOG = (
    "ghcr.io",
    "auth.docker.io",
    "registry-1.docker.io",
    "index.docker.io",
    "docker.io",
)


def classify_build_log(text: str) -> str | None:
    """Name the network phase a build log died or stalled in, if it says.

    Returns ``None`` when the log carries no registry evidence, so callers
    can stay silent instead of inventing a cause.  This only reads what
    Docker already printed; it never guesses from the absence of output.
    """

    if not text:
        return None
    lowered = text.lower()
    host = next(
        (name for name in _REGISTRY_HOSTS_IN_LOG if name in lowered), None,
    )
    where = f" reaching {host}" if host else ""
    if any(marker in lowered for marker in _DNS_MARKERS):
        return f"the build log shows a DNS failure{where}"
    if any(marker in lowered for marker in _TLS_MARKERS):
        return f"the build log shows a TLS failure{where}"
    if any(marker in lowered for marker in _AUTH_MARKERS):
        return f"the build log shows a registry authentication failure{where}"
    if any(marker in lowered for marker in _CONNECT_MARKERS):
        return f"the build log shows a connection timeout{where}"
    return None
