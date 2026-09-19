import socket
import time

from dradar import net_probe


class _Clock:
    """Deterministic stand-in so a probe's own budget can be asserted."""

    def __init__(self):
        self.now = 0.0

    def advance(self, seconds):
        self.now += seconds

    def __call__(self):
        return self.now


def test_resolver_error_is_reported_as_a_dns_failure(monkeypatch):
    def boom(*_args, **_kwargs):
        raise socket.gaierror(8, "nodename nor servname provided")

    monkeypatch.setattr(net_probe.socket, "getaddrinfo", boom)
    probe = net_probe.probe_host("ghcr.io")
    assert probe.stage == "dns"
    assert not probe.ok
    hint = net_probe.probe_hint(probe, "wsl")
    assert "did not resolve" in hint
    assert "resolv.conf" in hint


def test_a_resolver_that_never_answers_cannot_hang_the_probe(monkeypatch):
    """The diagnostic must not become the hang it exists to diagnose.

    ``getaddrinfo`` is uninterruptible once it reaches the platform
    resolver, so the probe runs it on an abandonable thread.  Assert the
    call returns within its own budget even though the resolver never does.
    """

    release = []

    def never_answers(*_args, **_kwargs):
        stop = []
        release.append(stop)
        while not stop:
            time.sleep(0.01)
        return []

    monkeypatch.setattr(net_probe.socket, "getaddrinfo", never_answers)
    started = time.monotonic()
    probe = net_probe.probe_host("ghcr.io", dns_timeout=0.3)
    elapsed = time.monotonic() - started
    for stop in release:
        stop.append(True)
    assert probe.stage == "dns"
    assert "no answer within" in probe.detail
    assert elapsed < 3.0


def test_tls_failure_keeps_dns_success_visible(monkeypatch):
    monkeypatch.setattr(
        net_probe.socket, "getaddrinfo",
        lambda *a, **k: [(None, None, None, None, ("140.82.121.33", 443))],
    )
    monkeypatch.setattr(
        net_probe, "_tls_handshake",
        lambda host, timeout: (False, 12.0, "TimeoutError: timed out"),
    )
    probe = net_probe.probe_host("ghcr.io")
    assert probe.stage == "tls"
    assert probe.dns_ms is not None
    assert "TLS handshake did not complete" in net_probe.probe_hint(probe, "linux")


def test_only_a_verification_failure_is_called_interception():
    """A dead connection is not evidence of a bad certificate.

    Reporting SSLEOFError as "unexpected certificate" sends the volunteer
    after a CA problem that is not there.
    """

    dead = net_probe.HostProbe("ghcr.io", "tls", detail="SSLEOFError: EOF occurred")
    assert "did not verify" not in net_probe.probe_hint(dead, "linux")
    intercepted = net_probe.HostProbe(
        "ghcr.io", "tls", detail="SSLCertVerificationError: certificate verify failed",
    )
    assert "did not verify" in net_probe.probe_hint(intercepted, "linux")


def test_a_synthetic_dns_pool_is_not_reported_as_a_healthy_resolver(monkeypatch):
    """clash/mihomo answer every name from a fake-IP pool.

    On such a machine a nonexistent host still resolves, so "DNS ok" would
    be a false reassurance; say the address proves nothing instead.
    """

    monkeypatch.setattr(
        net_probe.socket, "getaddrinfo",
        lambda *a, **k: [(None, None, None, None, ("198.18.0.114", 443))],
    )
    monkeypatch.setattr(
        net_probe, "_tls_handshake",
        lambda host, timeout: (False, 9.0, "TimeoutError: timed out"),
    )
    probe = net_probe.probe_host("ghcr.io")
    assert probe.synthetic_dns
    assert "synthetic DNS pool" in net_probe.probe_hint(probe, "linux")


def test_proxy_detection_downgrades_a_direct_verdict():
    assert net_probe.proxy_configured({"HTTPS_PROXY": "http://127.0.0.1:7890"}) == (
        "HTTPS_PROXY"
    )
    assert net_probe.proxy_configured({"HTTPS_PROXY": "  "}) is None
    assert net_probe.proxy_configured({}) is None


def test_both_registries_pier_needs_are_probed():
    assert "ghcr.io" in net_probe.REGISTRY_PROBE_HOSTS
    assert "auth.docker.io" in net_probe.REGISTRY_PROBE_HOSTS


def test_build_log_classifier_names_the_phase():
    dns = net_probe.classify_build_log(
        'failed to do request: Head "https://ghcr.io/v2/x/manifests/y": '
        "dial tcp: lookup ghcr.io on 172.29.240.1:53: no such host"
    )
    assert dns == "the build log shows a DNS failure reaching ghcr.io"
    tls = net_probe.classify_build_log(
        'failed to fetch anonymous token: Get "https://auth.docker.io/token": '
        "net/http: TLS handshake timeout"
    )
    assert tls == "the build log shows a TLS failure reaching auth.docker.io"
    intercepted = net_probe.classify_build_log(
        "x509: certificate is valid for *.corp.local, not registry-1.docker.io"
    )
    assert "TLS failure" in intercepted


def test_classifier_stays_silent_without_registry_evidence():
    """Negative control: a healthy build must not be diagnosed as a network
    failure, and an empty log must not produce a cause at all."""

    assert net_probe.classify_build_log("") is None
    assert net_probe.classify_build_log(
        "#5 [3/9] RUN pip install -r requirements.txt\n#5 DONE 41.2s"
    ) is None
    assert net_probe.classify_build_log(
        "pytest: 3 failed, 110 passed"
    ) is None


def test_summary_reports_each_host_and_its_timings():
    probes = [
        net_probe.HostProbe("ghcr.io", "ok", dns_ms=5.0, tls_ms=228.0),
        net_probe.HostProbe("auth.docker.io", "dns", dns_ms=5000.0),
    ]
    summary = net_probe.summarize(probes)
    assert "ghcr.io ok (dns 5ms, tls 228ms)" in summary
    assert "auth.docker.io DNS failed" in summary
