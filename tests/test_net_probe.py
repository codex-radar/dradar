import socket
import ssl
import time

import pytest

from dradar import net_probe


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
        lambda host, addrs, timeout: (False, 12.0, "TimeoutError: timed out"),
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
        lambda host, addrs, timeout: (False, 9.0, "TimeoutError: timed out"),
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


def test_mixed_stage_failures_are_not_flattened_onto_one_phase():
    """A firewall that resolves one registry and blocks another at a
    different layer is the normal shape of a partial outage."""

    hint = net_probe.failure_hint([
        net_probe.HostProbe("ghcr.io", "dns", detail="no answer within 7s"),
        net_probe.HostProbe("auth.docker.io", "tls", detail="TimeoutError"),
    ], "linux")
    assert "ghcr.io did not resolve" in hint
    assert "auth.docker.io did not finish TLS" in hint
    assert "auth.docker.io did not resolve" not in hint


def test_the_tls_step_never_resolves_a_second_time(monkeypatch):
    """`socket.create_connection` would call getaddrinfo again, off the
    abandonable thread and outside every budget -- a resolver that answers
    once then stalls made the whole probe unbounded through it."""

    calls = []
    real = net_probe.socket.getaddrinfo

    def counted(*args, **kwargs):
        calls.append(args[0])
        return [(net_probe.socket.AF_INET, None, None, None, ("127.0.0.1", 443))]

    monkeypatch.setattr(net_probe.socket, "getaddrinfo", counted)
    monkeypatch.setattr(
        net_probe, "_tls_context", lambda: _RefusingContext(),
    )
    net_probe.probe_host("ghcr.io", dns_timeout=1.0, tls_timeout=1.0)
    assert calls == ["ghcr.io"], f"resolver was consulted {len(calls)} times"
    assert real is not None


class _RefusingContext:
    def wrap_socket(self, sock, server_hostname=None):
        raise OSError("refused")


def test_the_whole_probe_stays_within_budget_when_the_resolver_stalls_midway(
    monkeypatch,
):
    """Measure a real probe instead of asserting arithmetic on constants.

    The first lookup succeeds and the second never returns -- the behaviour
    of an intermittent resolver, and previously unbounded.
    """

    state = {"n": 0}
    stop = []

    def flaky(*_args, **_kwargs):
        state["n"] += 1
        if state["n"] == 1:
            return [(net_probe.socket.AF_INET, None, None, None, ("127.0.0.1", 443))]
        while not stop:
            time.sleep(0.01)
        return []

    monkeypatch.setattr(net_probe.socket, "getaddrinfo", flaky)
    started = time.monotonic()
    net_probe.probe_host("ghcr.io", dns_timeout=1.0, tls_timeout=1.0)
    elapsed = time.monotonic() - started
    stop.append(True)
    assert elapsed < 4.0, f"probe ran {elapsed:.1f}s against a 2.0s budget"


def test_the_trust_store_is_the_system_one_plus_certifi():
    """Passing cafile= to create_default_context REPLACES the system store,
    hiding the CA that corporate TLS inspection installs at the OS level and
    failing a machine that works."""

    system = ssl.create_default_context()
    ours = net_probe._tls_context()
    system_subjects = {c["subject"] for c in system.get_ca_certs()}
    our_subjects = {c["subject"] for c in ours.get_ca_certs()}
    assert system_subjects <= our_subjects, (
        f"{len(system_subjects - our_subjects)} system-trusted CAs are "
        "invisible to the probe"
    )


def test_an_idna_illegal_hostname_does_not_escape_as_a_thread_traceback():
    """getaddrinfo raises UnicodeError, not OSError, for such a name."""

    probe = net_probe.probe_host("\u0080" * 100, dns_timeout=1.0)
    assert probe.stage == "dns"
    assert probe.detail


def test_dns_budget_avoids_the_stock_resolver_retry_interval():
    """A stock resolv.conf carries `options timeout:5 attempts:2`, so a
    machine whose first nameserver is dead answers just after 5s."""

    assert net_probe.DNS_TIMEOUT_SEC > 5.0


def test_an_answer_landing_on_the_budget_is_not_called_a_timeout(monkeypatch):
    """An answer inside the budget must never be called a timeout.

    Reading `worker.is_alive()` before reading the result could discard an
    answer the thread had already written. An answer landing *exactly* on
    the budget stays a genuine coin flip -- that is the definition of a
    deadline, not a defect -- which is why DNS_TIMEOUT_SEC was also moved
    off the stock resolver retry interval where that coin was flipped
    systematically (#0152 QA suggestion 2).
    """

    def slow(*_args, **_kwargs):
        time.sleep(0.1)
        return [(net_probe.socket.AF_INET, None, None, None, ("127.0.0.1", 443))]

    monkeypatch.setattr(net_probe.socket, "getaddrinfo", slow)
    misses = 0
    for _ in range(40):
        addrs, _ms, detail = net_probe._resolve("ghcr.io", 0.3)
        if addrs is None and "no answer" in detail:
            misses += 1
    assert misses == 0, f"{misses}/40 in-budget answers reported as timeouts"


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


# Real build output that contains a failure marker, a registry host, or both,
# but is NOT a registry failure. Every one of these produced a confident wrong
# diagnosis before the classifier became line-oriented (#0152 QA).
NOT_A_REGISTRY_FAILURE = (
    ("空日志", ""),
    ("健康构建", "#5 [3/9] RUN pip install -r reqs.txt\n#5 DONE 41.2s"),
    ("测试摘要", "pytest: 3 failed, 110 passed"),
    (
        "Gradle 依赖冲突,且成功拉过 ghcr 镜像",
        "#1 [internal] load metadata for ghcr.io/codex-radar/"
        "dradar-egress-proxy@sha256:abc\n#1 DONE 11.4s\n"
        "FAILURE: Could not resolve all dependencies for ':runtimeClasspath'",
    ),
    (
        "链接器符号未解析",
        "ld: could not resolve symbol _ZN3foo3barEv",
    ),
    (
        "Go 模块查找被关闭(完全健康)",
        "go: module lookup disabled by GOFLAGS=-mod=vendor",
    ),
    (
        "等数据库起来的正常重试",
        "psql: error: connection to server at localhost port 5432 failed: "
        "Connection refused",
    ),
    (
        "磁盘满,与 ghcr 无关",
        "#1 [internal] load metadata for ghcr.io/codex-radar/proxy\n#1 DONE 9.1s\n"
        "#7 ERROR: write /var/lib/docker/tmp/x: no space left on device\n"
        "context deadline exceeded",
    ),
    (
        "被测任务自己在测 DNS,断言通过",
        "#9 [5/9] RUN pytest tests/test_resolver.py\n"
        "test_bad_host[lookup example.invalid: no such host] PASSED",
    ),
    (
        "编译失败,而日志早先拉过 ghcr",
        "#1 [internal] load metadata for ghcr.io/codex-radar/proxy\n#1 DONE 8.0s\n"
        '#12 ERROR: process "/bin/sh -c make" did not complete successfully',
    ),
)


@pytest.mark.parametrize(
    "label,log", NOT_A_REGISTRY_FAILURE, ids=[n for n, _ in NOT_A_REGISTRY_FAILURE],
)
def test_classifier_stays_silent_without_registry_evidence(label, log):
    """A wrong diagnosis is worse than none: the volunteer acts on it."""

    assert net_probe.classify_build_log(log) is None, label


def test_a_successful_ghcr_pull_does_not_steal_the_blame():
    """The pinned egress image is the first thing every build pulls, so a
    whole-window scan attributed a Docker Hub failure to ghcr.io."""

    verdict = net_probe.classify_build_log(
        "#1 [internal] load metadata for ghcr.io/codex-radar/"
        "dradar-egress-proxy@sha256:abc\n#1 DONE 11.4s\n"
        '#3 ERROR: failed to fetch anonymous token: Get "https://auth.docker.io'
        '/token": net/http: TLS handshake timeout'
    )
    assert verdict == (
        "the build log shows a TLS failure reaching auth.docker.io"
    )


def test_the_newest_registry_failure_wins():
    """A build that retried prints the operative failure last."""

    verdict = net_probe.classify_build_log(
        "#2 ERROR: Head \"https://ghcr.io/v2/x\": dial tcp: lookup ghcr.io: "
        "no such host\n"
        "#2 retrying\n"
        '#2 ERROR: Get "https://ghcr.io/v2/x": x509: certificate signed by '
        "unknown authority"
    )
    assert verdict == "the build log shows a TLS failure reaching ghcr.io"


def test_summary_reports_each_host_and_its_timings():
    probes = [
        net_probe.HostProbe("ghcr.io", "ok", dns_ms=5.0, tls_ms=228.0),
        net_probe.HostProbe("auth.docker.io", "dns", dns_ms=5000.0),
    ]
    summary = net_probe.summarize(probes)
    assert "ghcr.io ok (dns 5ms, tls 228ms)" in summary
    assert "auth.docker.io DNS failed" in summary


def test_combined_hint_names_every_host_but_gives_advice_once():
    probes = [
        net_probe.HostProbe("ghcr.io", "dns", detail="no answer within 5s"),
        net_probe.HostProbe("auth.docker.io", "dns", detail="no answer within 5s"),
    ]
    hint = net_probe.failure_hint(probes, "wsl")
    assert hint.startswith("ghcr.io, auth.docker.io did not resolve")
    assert hint.count("generateResolvConf=false") == 1


def test_combined_hint_is_empty_when_everything_works():
    probes = [net_probe.HostProbe("ghcr.io", "ok", dns_ms=5.0, tls_ms=228.0)]
    assert net_probe.failure_hint(probes, "linux") == ""


def test_advice_separates_interception_from_a_dead_connection():
    dead = net_probe.HostProbe("ghcr.io", "tls", detail="TimeoutError: timed out")
    assert "outbound 443" in net_probe.probe_advice(dead, "linux")
    intercepted = net_probe.HostProbe(
        "ghcr.io", "tls", detail="SSLCertVerificationError: certificate verify failed",
    )
    assert "trust its CA" in net_probe.probe_advice(intercepted, "linux")


# The host must be the one the error is ABOUT -- the URL it fetched, the name
# it looked up, the address it dialled -- not merely a registry name present
# somewhere on the line. Each of these was a wrong answer when any mention
# counted (#0152 QA round 2).
OPERAND_CASES = (
    (
        "配了镜像源:不通的是镜像站,不是 docker.io",
        '#2 ERROR: failed to resolve source metadata for docker.io/library/'
        'ubuntu:24.04: failed to do request: Head "https://dockerproxy.cn/v2/'
        'library/ubuntu/manifests/24.04": dial tcp 1.2.3.4:443: i/o timeout',
        None,
    ),
    (
        "一行两个 registry,失败的不是优先级靠前那个",
        'COPY --from=ghcr.io/codex-radar/base /opt /opt\n'
        '#4 ERROR: failed to fetch anonymous token: Get '
        '"https://auth.docker.io/token": net/http: TLS handshake timeout',
        "auth.docker.io",
    ),
    (
        "镜像内的探活循环提到了 registry 名",
        "#5 0.2 probing ghcr.io ... connection refused, retry 1/30",
        None,
    ),
    (
        "curl 的 URL 里恰好含 registry 名,失败的是别的主机",
        '#6 curl: (6) Could not resolve host: raw.githubusercontent.com\n'
        '#6 + curl -fsSL https://raw.githubusercontent.com/x/ghcr.io-notes.md',
        None,
    ),
    (
        "真阳性:确实是 auth.docker.io 解析失败",
        'ERROR: failed to solve: docker.io/library/ubuntu:24.04: failed to '
        'authorize: failed to fetch anonymous token: Get "https://'
        'auth.docker.io/token?scope=repository": dial tcp: lookup '
        'auth.docker.io: no such host',
        "auth.docker.io",
    ),
)


@pytest.mark.parametrize(
    "label,log,expected", OPERAND_CASES, ids=[c[0] for c in OPERAND_CASES],
)
def test_the_host_is_taken_from_the_failure_operand(label, log, expected):
    verdict = net_probe.classify_build_log(log)
    if expected is None:
        assert verdict is None, f"{label}: 误报 {verdict}"
    else:
        assert verdict is not None, f"{label}: 漏报"
        assert expected in verdict, f"{label}: {verdict}"


def test_a_long_certificate_san_list_cannot_stall_the_classifier():
    """A regex spanning the comma-separated SAN list nested one quantifier
    inside another and backtracked exponentially -- 2x per comma, hours at
    forty. Certificates routinely carry dozens of SANs, and this runs on
    every heartbeat, so it would have become the hang it reports (#0152 QA).
    """

    pathological = (
        "x509: certificate is valid for " + ", ".join(["a"] * 2000) + " END"
    )
    started = time.monotonic()
    assert net_probe.classify_build_log(pathological) is None
    assert time.monotonic() - started < 0.5


def test_a_real_certificate_mismatch_is_still_named():
    verdict = net_probe.classify_build_log(
        "x509: certificate is valid for *.corp.local, *.a.com, *.b.com, "
        "not registry-1.docker.io"
    )
    assert verdict == "the build log shows a TLS failure reaching registry-1.docker.io"


def test_a_bare_not_is_not_a_failure_operand():
    """'not' is an ordinary English word; only the x509 phrase licenses it."""

    assert net_probe.classify_build_log(
        "#4 ERROR: image is not ghcr.io/foo, connection refused"
    ) is None


def test_one_enormous_line_is_truncated_before_matching():
    huge = "#9 " + "x" * 200_000 + ' Head "https://ghcr.io/v2/x": no such host'
    started = time.monotonic()
    net_probe.classify_build_log(huge)
    assert time.monotonic() - started < 0.5


# Splitting the anchored x509 regex to kill its exponential backtracking
# dropped the anchor: the phrase and the ", not <host>" clause were then
# tested independently anywhere on the line (#0152 QA r3).
X509_ANCHOR_CASES = (
    (
        "not 子句在短语之前,与它无关",
        "#4 mirror selected, not ghcr.io; later: x509: certificate is "
        "valid for *.corp.local, not internal.corp",
        None,
    ),
    (
        "短语之后是普通英文的 not",
        "x509: certificate is valid for internal.corp, not internal2.corp "
        "(this affects the proxy, not ghcr.io which is fine)",
        None,
    ),
    (
        "被 COPY 进镜像的文档原文,没有 x509:",
        'COPY NOTES.md: "our certificate is valid for the mirror, '
        'not ghcr.io - see wiki"',
        None,
    ),
    (
        "真阳性:确实是证书不匹配",
        "x509: certificate is valid for *.corp.local, not registry-1.docker.io",
        "registry-1.docker.io",
    ),
)


@pytest.mark.parametrize(
    "label,log,expected", X509_ANCHOR_CASES, ids=[c[0] for c in X509_ANCHOR_CASES],
)
def test_the_x509_clause_must_belong_to_the_x509_error(label, log, expected):
    verdict = net_probe.classify_build_log(log)
    if expected is None:
        assert verdict is None, f"{label}: 误报 {verdict}"
    else:
        assert verdict is not None and expected in verdict, f"{label}: {verdict}"


def test_the_x509_anchor_did_not_reintroduce_backtracking():
    for payload in (
        "x509: certificate is valid for " + ", ".join(["a"] * 20000),
        "x509: " + "certificate is valid for x, not !" * 5000,
        "x509: certificate is valid for " + "a." * 50000,
    ):
        started = time.monotonic()
        net_probe.classify_build_log(payload)
        assert time.monotonic() - started < 0.5


def test_a_long_wrapped_line_keeps_its_proximate_cause():
    """Go wraps causes from the outside in, so the real failure is at the
    END of a long line. Truncating only the head silently dropped it."""

    line = (
        "#3 ERROR: failed to solve: " + "wrapped: " * 600
        + 'Head "https://ghcr.io/v2/x": dial tcp: lookup ghcr.io: no such host'
    )
    assert len(line) > net_probe._MAX_CLASSIFIED_LINE
    assert net_probe.classify_build_log(line) == (
        "the build log shows a DNS failure reaching ghcr.io"
    )


def test_the_phase_comes_from_the_proximate_marker_not_a_fixed_order():
    """Go wraps causes outside-in, so the rightmost marker is the real one.

    A fixed DNS>TLS>AUTH>CONNECT precedence chose the phase independently
    of the host, so a non-registry DNS complaint alongside a genuine
    registry TLS failure reported the right host with the wrong layer --
    sending the volunteer to fix a resolver that works, while `dradar
    doctor` reports that resolver healthy (#0152 QA r3).
    """

    verdict = net_probe.classify_build_log(
        "ERROR: could not resolve mirror.corp.local; falling back: "
        'Get "https://auth.docker.io/token": net/http: TLS handshake timeout'
    )
    assert verdict == "the build log shows a TLS failure reaching auth.docker.io"


def test_a_single_marker_line_keeps_its_obvious_phase():
    """Negative control for the change above: with one marker there is no
    precedence question, and the phase must not drift."""

    assert net_probe.classify_build_log(
        '#2 ERROR: Head "https://ghcr.io/v2/x": dial tcp: lookup ghcr.io: '
        "no such host"
    ) == "the build log shows a DNS failure reaching ghcr.io"
    assert net_probe.classify_build_log(
        'failed to fetch anonymous token: Get "https://auth.docker.io/token": '
        "net/http: TLS handshake timeout"
    ) == "the build log shows a TLS failure reaching auth.docker.io"


# BuildKit and containerd append a generic "ran out of time" note *after* the
# real cause, so the rightmost marker is often the least specific one. Each of
# these reported "a connection timeout" once the phase was taken from the
# rightmost marker -- including the failure this ticket exists for (#0152 QA r4).
TRAILING_NOTE_CASES = (
    (
        "DNS 失败 + BuildKit 尾缀(apmengzi 的原始形态)",
        '#2 Head "https://auth.docker.io/v2/": dial tcp: lookup '
        "auth.docker.io: no such host: context deadline exceeded",
        "a DNS failure reaching auth.docker.io",
    ),
    (
        "证书被拦截 + 尾缀(模块唯一的断言式结论)",
        '#3 Get "https://ghcr.io/v2/": x509: certificate signed by unknown '
        "authority: context deadline exceeded",
        "a TLS failure reaching ghcr.io",
    ),
    (
        "TLS 握手超时 + 尾缀",
        '#4 Get "https://ghcr.io/v2/": net/http: TLS handshake timeout: '
        "context deadline exceeded",
        "a TLS failure reaching ghcr.io",
    ),
    (
        "纯连接超时:必须仍然是 connection timeout",
        '#5 Head "https://ghcr.io/v2/": dial tcp 1.2.3.4:443: i/o timeout',
        "a connection timeout reaching ghcr.io",
    ),
    (
        "只有尾缀、没有更具体的:兜底仍然报连接超时",
        '#6 Head "https://ghcr.io/v2/": context deadline exceeded',
        "a connection timeout reaching ghcr.io",
    ),
    (
        "另一个泛型尾注单独出现时同样走兜底",
        '#7 Head "https://ghcr.io/v2/": i/o timeout',
        "a connection timeout reaching ghcr.io",
    ),
)

# The fallback tier must not start guessing a subject. It fires only when no
# specific marker matched, which is exactly when there is least evidence.
FALLBACK_MUST_STAY_SILENT = (
    (
        "泛型超时,但操作数不是 registry",
        '#8 Head "https://mirror.corp.local/v2/": context deadline exceeded',
    ),
    (
        "泛型超时,操作数是镜像站",
        '#9 Get "https://dockerproxy.cn/v2/": i/o timeout',
    ),
    (
        "泛型超时,行内根本没有操作数",
        "#10 build step timed out: context deadline exceeded",
    ),
)


@pytest.mark.parametrize(
    "label,log", FALLBACK_MUST_STAY_SILENT,
    ids=[c[0] for c in FALLBACK_MUST_STAY_SILENT],
)
def test_the_fallback_tier_still_needs_a_registry_operand(label, log):
    assert net_probe.classify_build_log(log) is None, label


@pytest.mark.parametrize(
    "label,log,expected", TRAILING_NOTE_CASES,
    ids=[c[0] for c in TRAILING_NOTE_CASES],
)
def test_a_trailing_timeout_note_does_not_override_a_specific_cause(
    label, log, expected,
):
    assert net_probe.classify_build_log(log) == (
        f"the build log shows {expected}"
    ), label


def test_the_classified_prefix_is_shared_not_duplicated():
    """runner._historical rewrites this opening; a literal at each end would
    drift apart silently."""

    verdict = net_probe.classify_build_log(
        "#2 dial tcp: lookup ghcr.io: no such host"
    )
    assert verdict.startswith(net_probe.CLASSIFIED_PREFIX)
