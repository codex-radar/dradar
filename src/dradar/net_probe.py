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
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass

import certifi

# A healthy resolver answers in well under a second.  Seven seconds covers a
# cold cache behind a VPN and deliberately avoids 5.0: a stock
# ``resolv.conf`` carries ``options timeout:5 attempts:2``, so a machine
# whose first nameserver is dead answers at just over five seconds -- a
# five-second budget would fail exactly the machines that still work.
# ``TLS_TIMEOUT_SEC`` covers connect *and* handshake across every address
# tried, so two hosts cost at most 30 s, against the 6 min of silence
# measured on the current build path (#0152).
DNS_TIMEOUT_SEC = 7.0
TLS_TIMEOUT_SEC = 8.0
# Registries publish A and AAAA records; trying every one on a host with no
# route to that family would multiply the budget. The budget is shared, so
# this only bounds how finely it is divided.
_MAX_TLS_ADDRESSES = 3

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


def _resolve(
    host: str, timeout: float,
) -> tuple[list[tuple[int, str]] | None, float, str]:
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
            # Keep the family with each address: the TLS step connects to
            # these directly and must not ask the resolver a second time.
            result["addrs"] = sorted({(info[0], info[4][0]) for info in infos})
        except (OSError, UnicodeError) as exc:
            # getaddrinfo raises UnicodeError, not OSError, for a name IDNA
            # cannot encode. Letting that escape would print a thread
            # traceback over the volunteer's report.
            result["error"] = f"{type(exc).__name__}: {exc}"

    worker = threading.Thread(
        target=run, name=f"dradar-dns-{host}", daemon=True,
    )
    started = time.monotonic()
    worker.start()
    worker.join(timeout)
    elapsed_ms = (time.monotonic() - started) * 1000.0
    # Read the answer before asking whether the thread is alive: ``run``
    # writes the result before it returns, so a thread that has answered can
    # still be alive for a moment and must not be called a timeout.
    #
    # An answer landing *exactly* on the budget remains a coin flip -- that
    # is what a deadline is. What matters is not flipping that coin
    # systematically, which is why DNS_TIMEOUT_SEC sits away from the stock
    # resolver retry interval rather than on it.
    addrs = result.get("addrs")
    if isinstance(addrs, list) and addrs:
        return addrs, elapsed_ms, ""
    error = result.get("error")
    if error is not None:
        return None, elapsed_ms, str(error)
    if worker.is_alive():
        return None, elapsed_ms, f"no answer within {timeout:g}s"
    return None, elapsed_ms, "resolver returned no address"


def _tls_context() -> ssl.SSLContext:
    """Trust the system store *and* certifi, never certifi alone.

    Passing ``cafile=`` to ``create_default_context`` replaces the system
    trust store rather than adding to it -- 39 CAs the OS trusts were
    invisible to this probe on the development machine. Corporate and campus
    TLS inspection installs its CA at the OS level, so certifi-only made a
    healthy machine fail with "certificate verify failed", which is the one
    verdict this module states as a certainty.
    """

    context = ssl.create_default_context()
    try:
        context.load_verify_locations(cafile=certifi.where())
    except (OSError, ssl.SSLError):
        # A system store that already works is enough; certifi is a bonus.
        pass
    return context


def _tls_handshake(
    host: str, addrs: list[tuple[int, str]], timeout: float,
) -> tuple[bool, float, str]:
    """Handshake with ``host`` over an already-resolved address.

    Connects to the resolved IP directly instead of via
    ``socket.create_connection``, which would call ``getaddrinfo`` again --
    off the abandonable thread and outside every budget this module sets. A
    resolver that answers once and then stalls (WSL2 NAT, a flaky hotspot)
    made the whole probe unbounded through that second lookup: the exact
    hang this module exists to diagnose.

    ``timeout`` is the budget for connect *and* handshake together, across
    every address tried, so the caller's arithmetic holds.
    """

    context = _tls_context()
    started = time.monotonic()
    deadline = started + timeout
    detail = "no address could be reached"
    for family, address in addrs[:_MAX_TLS_ADDRESSES]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.settimeout(remaining)
            sock.connect((address, 443))
            sock.settimeout(max(0.05, deadline - time.monotonic()))
            with context.wrap_socket(sock, server_hostname=host):
                return True, (time.monotonic() - started) * 1000.0, ""
        except (OSError, ssl.SSLError) as exc:
            detail = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                sock.close()
            except OSError:
                pass
    if time.monotonic() >= deadline and "timed out" not in detail.lower():
        detail = f"no handshake within {timeout:g}s ({detail})"
    return False, (time.monotonic() - started) * 1000.0, detail


def probe_host(
    host: str,
    *,
    dns_timeout: float | None = None,
    tls_timeout: float | None = None,
) -> HostProbe:
    """Resolve ``host`` and complete a TLS handshake, each within budget.

    The budgets default to the module constants *at call time*. Binding them
    as default arguments froze them at import, so neither an operator nor a
    test could change them -- a budget test that patched the constants
    silently exercised the real 7s/8s instead (#0152 QA).
    """

    dns_timeout = DNS_TIMEOUT_SEC if dns_timeout is None else dns_timeout
    tls_timeout = TLS_TIMEOUT_SEC if tls_timeout is None else tls_timeout
    addrs, dns_ms, dns_error = _resolve(host, dns_timeout)
    if addrs is None:
        return HostProbe(host, "dns", dns_ms=dns_ms, detail=dns_error)
    synthetic = any(
        address.startswith(prefix)
        for _family, address in addrs
        for prefix in _FAKE_IP_PREFIXES
    )
    tls_ok, tls_ms, tls_error = _tls_handshake(host, addrs, tls_timeout)
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
    dns_timeout: float | None = None,
    tls_timeout: float | None = None,
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
    """One actionable line per failing phase, advice not repeated.

    Hosts are grouped by the phase that failed. Flattening them onto the
    first host's phase told a volunteer whose ``auth.docker.io`` handshake
    timed out that it "did not resolve", and handed them a resolver recipe
    for a resolver that was working -- firewalls that permit one registry
    and block another at a different layer make this the normal shape of a
    partial outage, not a corner case (#0152 QA).
    """

    failed = [probe for probe in probes if not probe.ok]
    if not failed:
        return ""
    parts = []
    for stage, wording in (("dns", "did not resolve"), ("tls", "did not finish TLS")):
        group = [probe for probe in failed if probe.stage == stage]
        if not group:
            continue
        hosts = ", ".join(probe.host for probe in group)
        advice = "; ".join(
            dict.fromkeys(probe_advice(probe, platform) for probe in group)
        )
        parts.append(f"{hosts} {wording} ({group[0].detail}); {advice}")
    return " | ".join(parts)


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
#
# Every one of these also occurs in ordinary build output that has nothing to
# do with a registry -- Gradle prints "Could not resolve all dependencies",
# the linker prints "could not resolve symbol", a service wait loop prints
# "Connection refused", and a task's own test suite may print "no such host"
# as a passing assertion. They are therefore only ever consulted on a line
# that also names a registry host; see classify_build_log.
_DNS_MARKERS = (
    "no such host",
    "server misbehaving",
    "temporary failure in name resolution",
    "name resolution",
    "could not resolve",
)
_TLS_MARKERS = (
    "tls handshake timeout",
    "tls: handshake failure",
    "x509:",
    "certificate is valid for",
    "certificate signed by unknown authority",
)
_CONNECT_MARKERS = (
    "connection refused",
    "network is unreachable",
    "no route to host",
    "dial tcp",
)
# BuildKit and containerd append these *after* the real cause, as a generic
# note that the surrounding operation also ran out of time. They are the one
# place where "the rightmost marker is the proximate cause" is false, so they
# only decide the phase when nothing more specific matched: otherwise a
# trailing ": context deadline exceeded" rewrote `lookup X: no such host`
# into a connection timeout -- inverting the diagnosis for the very failure
# this module was written for, and demoting the module's one certain verdict
# (an intercepting proxy) to a generic timeout (#0152 QA r4).
_GENERIC_TIMEOUT_MARKERS = (
    "i/o timeout",
    "context deadline exceeded",
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


# The host an error names as the thing it was contacting. Docker and Go
# phrase that operand as a URL, as ``lookup X``, or as ``dial tcp X:port``;
# a registry name appearing anywhere else on the line is context, not the
# subject of the failure -- an image reference, a copy source, a structured
# log field, a probe message, or simply text inside a file being built.
_OPERAND_RE = re.compile(
    r"(?:https?://|lookup\s+|dial\s+tcp\s+)([a-z0-9][a-z0-9.\-]*\.[a-z]{2,})",
)
# Go's x509 mismatch names the host it was trying to reach after "not":
#   x509: certificate is valid for *.corp.local, not registry-1.docker.io
# That trailing name is the operand, so it is read the same way.
#
# The phrase is matched with a plain substring test and the host with a flat
# pattern, deliberately. Spanning the comma-separated SAN list with a regex
# (``[^,]+(?:,\s*[^,]+?)*``) nests one quantifier inside another, which
# backtracks exponentially when the line does not match: measured at 2x per
# comma, 0.28s at 22 commas and hours at 40 -- and certificates routinely
# carry dozens of SANs. That would have hung the CLI on every heartbeat,
# making this diagnostic the hang it exists to report (#0152 QA r3).
_X509_PHRASE = "certificate is valid for "
_X509_NOT_RE = re.compile(r",\s*not\s+([a-z0-9][a-z0-9.\-]*\.[a-z]{2,})")
# One pathological line must not cost more than it can possibly inform.
_MAX_CLASSIFIED_LINE = 4000
# The opening of every verdict this module returns. ``runner`` rewrites it
# when restating a cause as history, so the coupling is named here rather
# than duplicated as a literal at the other end.
CLASSIFIED_PREFIX = "the build log shows"


def _failing_registry(lowered: str) -> str | None:
    """The registry this line's error is *about*, if it is about one.

    Returning None for a line whose failure operand is some other host is
    the point: with a mirror configured -- standard for volunteers behind a
    slow path to Docker Hub -- the unreachable host is the mirror, while the
    canonical ``docker.io`` reference sits on the same line as plain text.
    Blaming ``docker.io`` there contradicts the live probe, which finds it
    healthy, and sends the volunteer to fix the wrong thing.
    """

    operands = _OPERAND_RE.findall(lowered)
    # Only Go's actual x509 error licenses reading a host after "not", and
    # only the first such clause *after* the phrase is its operand. Testing
    # the phrase and the clause independently anywhere on the line -- which
    # is what splitting the anchored regex to kill its backtracking left
    # behind -- let an unrelated ", not ghcr.io" earlier in the line, or an
    # ordinary English "not ghcr.io, which is fine" later, become the
    # subject (#0152 QA r3).
    if "x509:" in lowered:
        phrase_at = lowered.find(_X509_PHRASE)
        if phrase_at != -1:
            after = lowered[phrase_at + len(_X509_PHRASE):]
            found = _X509_NOT_RE.search(after)
            if found is not None:
                operands.append(found.group(1))
    for host in reversed(operands):
        if any(
            host == known or host.endswith("." + known)
            for known in _REGISTRY_HOSTS_IN_LOG
        ):
            return host
    return None


def _bounded(line: str) -> str:
    """Cap one line's length, keeping both ends.

    Go and Docker wrap causes from the outside in, so the proximate failure
    sits at the *end* of a long line while the step prefix sits at the
    start. Truncating the head alone silently dropped the cause on a 4.5 KB
    wrapped error; keeping both ends preserves whichever one carries it.
    """

    if len(line) <= _MAX_CLASSIFIED_LINE:
        return line
    joiner = " … "
    head = _MAX_CLASSIFIED_LINE // 4
    tail = _MAX_CLASSIFIED_LINE - head - len(joiner)
    return line[:head] + joiner + line[-tail:]


def _classify_line(lowered: str) -> tuple[str, str] | None:
    """Phase and host for a line whose failure is about a registry.

    The phase is taken from the marker that appears *last* on the line, not
    from a fixed precedence over marker families. Go and Docker wrap causes
    from the outside in, so the proximate failure is the rightmost one. A
    fixed order picked the phase independently of the host, and a line
    carrying a non-registry DNS complaint alongside a real registry TLS
    failure came out as "a DNS failure reaching auth.docker.io" -- right
    host, wrong layer, sending the volunteer to fix a resolver that works
    while `dradar doctor` reports it healthy (#0152 QA r3).
    """

    phase, phase_at = None, -1
    for markers, name in (
        (_DNS_MARKERS, "a DNS failure"),
        (_TLS_MARKERS, "a TLS failure"),
        (_AUTH_MARKERS, "a registry authentication failure"),
        (_CONNECT_MARKERS, "a connection timeout"),
    ):
        for marker in markers:
            at = lowered.rfind(marker)
            if at > phase_at:
                phase, phase_at = name, at
    if phase is None:
        # Nothing specific said what went wrong; a generic timeout note is
        # then the only evidence there is, and it does mean the connection
        # never completed.
        if any(marker in lowered for marker in _GENERIC_TIMEOUT_MARKERS):
            phase = "a connection timeout"
        else:
            return None
    host = _failing_registry(lowered)
    return None if host is None else (phase, host)


def classify_build_log(text: str) -> str | None:
    """Name the network phase a build log died or stalled in, if it says.

    The failure marker and the registry host must appear on the **same
    line**, and the host named is the one on that line. Scanning the whole
    window instead attributed every failure to whichever host appeared
    first anywhere in it -- and since the pinned egress image on ``ghcr.io``
    is the first thing every build pulls, one *successful* ``ghcr.io`` pull
    stole the attribution from a real ``auth.docker.io`` failure, and any
    pre-registration failure at all (a compile error, a full disk) came out
    labelled as a network problem. In sixteen realistic logs the window
    scan produced fifteen confident wrong answers (#0152 QA).

    The newest evidence wins, because a build that retried prints the
    operative failure last. Returns ``None`` when no single line carries
    both, so callers stay silent rather than inventing a cause: a wrong
    diagnosis is worse than none, since the volunteer acts on it.
    """

    if not text:
        return None
    for line in reversed(text.splitlines()):
        found = _classify_line(_bounded(line.lower()))
        if found is not None:
            phase, host = found
            return f"{CLASSIFIED_PREFIX} {phase} reaching {host}"
    return None
