"""`dradar doctor` must check the registries Pier actually pulls from.

Before this check the only registry probe was ``docker build --check
--pull`` against ``docker.io/library/ubuntu``.  The pinned Pier egress
image lives on ``ghcr.io``, so a volunteer whose resolver could not answer
for that host got a clean doctor report and then a silent, failing build
(volunteer report, 2026-09-08).
"""

from dradar import doctor, net_probe


def _fixed(**by_host):
    def probe_host(host, **_kwargs):
        return by_host[host]
    return probe_host


def test_a_healthy_machine_reports_both_hosts_with_timings(monkeypatch, capsys):
    monkeypatch.setattr(net_probe, "probe_host", _fixed(**{
        "ghcr.io": net_probe.HostProbe("ghcr.io", "ok", dns_ms=5.0, tls_ms=228.0),
        "auth.docker.io": net_probe.HostProbe(
            "auth.docker.io", "ok", dns_ms=3.0, tls_ms=216.0,
        ),
    }))
    assert doctor._registry_reachability("linux") is True
    out = capsys.readouterr().out
    assert "[ok ]" in out
    assert "ghcr.io ok (dns 5ms, tls 228ms)" in out
    assert "auth.docker.io ok" in out


def test_a_broken_resolver_fails_with_the_platform_recipe(monkeypatch, capsys):
    """apmengzi's case: WSL2's NAT resolver answers for neither host."""

    dead = net_probe.HostProbe("", "dns", dns_ms=5000.0, detail="no answer within 5s")
    monkeypatch.setattr(net_probe, "probe_host", lambda host, **_k: (
        net_probe.HostProbe(host, dead.stage, dns_ms=dead.dns_ms, detail=dead.detail)
    ))
    monkeypatch.setattr(net_probe, "proxy_configured", lambda env=None: None)
    assert doctor._registry_reachability("wsl") is False
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "ghcr.io, auth.docker.io did not resolve" in out
    assert "generateResolvConf=false" in out
    # One recipe for two hosts that failed identically, not one per host.
    assert out.count("generateResolvConf=false") == 1


def test_only_the_failing_host_is_named(monkeypatch, capsys):
    """A ghcr.io-only outage was previously invisible: the old preflight
    exercised docker.io alone, so this half-failure reported as healthy."""

    monkeypatch.setattr(net_probe, "probe_host", _fixed(**{
        "ghcr.io": net_probe.HostProbe(
            "ghcr.io", "tls", dns_ms=4.0, tls_ms=8000.0,
            detail="TimeoutError: timed out",
        ),
        "auth.docker.io": net_probe.HostProbe(
            "auth.docker.io", "ok", dns_ms=3.0, tls_ms=210.0,
        ),
    }))
    monkeypatch.setattr(net_probe, "proxy_configured", lambda env=None: None)
    assert doctor._registry_reachability("windows") is False
    out = capsys.readouterr().out
    assert "ghcr.io did not finish TLS" in out
    assert "auth.docker.io did not" not in out


def test_a_configured_proxy_warns_instead_of_failing(monkeypatch, capsys):
    """The proxy, not this process, resolves the name, and Docker may carry
    a daemon-side proxy that is invisible here. Do not tell a volunteer with
    a working machine that it is broken."""

    monkeypatch.setattr(net_probe, "probe_host", lambda host, **_k: (
        net_probe.HostProbe(host, "dns", dns_ms=5000.0, detail="no answer within 5s")
    ))
    monkeypatch.setattr(net_probe, "proxy_configured", lambda env=None: "HTTPS_PROXY")
    assert doctor._registry_reachability("linux") is True
    out = capsys.readouterr().out
    assert "[warn]" in out
    assert "HTTPS_PROXY is set" in out
    assert "[FAIL]" not in out


def test_the_check_cannot_outlast_its_own_budget():
    """A doctor that hangs is worse than no doctor: the volunteer assumes it
    is still checking. Two hosts, each bounded twice."""

    worst_case = len(net_probe.REGISTRY_PROBE_HOSTS) * (
        net_probe.DNS_TIMEOUT_SEC + net_probe.TLS_TIMEOUT_SEC
    )
    assert worst_case <= 30
