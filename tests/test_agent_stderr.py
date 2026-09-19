"""Agent stderr forensics: capture the cause of a non-zero agent exit without
writing a credential into client_meta.

Every fixture credential below is an obvious fake. The suite's centre of
gravity is the negative control in `test_no_planted_credential_survives`: a
redactor that only proves "ordinary text passes" proves nothing.
"""
import json
import os
import re
from pathlib import Path

import pytest

import dradar.runloop as runloop
from dradar import agent_stderr
from dradar.agent_stderr import (
    MAX_REDACTED_CHARS, STDERR_CANDIDATES, agent_exited_non_zero,
    collect_agent_stderr, collect_for_agent_exit, redact_diagnostic_text,
)

from test_go_menu import ASSIGNMENT, SubmitClient, _args, _fake_art

NON_ZERO_EXIT = {
    "exception_info": {
        "exception_type": "NonZeroAgentExitCodeError",
        "exception_message": "Command failed (exit 1): antigravity --print",
    },
    "agent_result": {},
}
COMPLETED = {"agent_result": {"cost_usd": 0.0}}

# Deliberately fake, but each has the exact SHAPE of the real thing — that is
# what the redactor keys on.
PLANTED = {
    "openai-key": "sk-proj-abcdefghijklmnopqrstuvwxyz012345",
    "anthropic-key": "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAA",
    "github-oauth": "gho_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    "github-pat": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    "jwt": ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmYWtlIn0."
            "dBjftJeZ4CVPmB92K27uhbUJU1p1r0FakeSig"),
    "fernet-session": "gAAAAABm" + "Zk9" * 20,
    "aws-key-id": "AKIAIOSFODNN7EXAMPLE",
    "aws-secret": "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY",
    "hex-digest": "a3f5c9e2b8d1f4a7c0e3b6d9f2a5c8e1" * 2,
    "lowercase-hex": "deadbeefcafebabedeadbeefcafebabe",
    "base64-padded": "YWxvaGE6c3VwZXJzZWNyZXRwYXNzd29yZA==",
    "base64-with-slash": "abc/def+ghiJKLmnoPQRstuVWXyz0123456789AB",
    "shaped-opaque": "AbcDefGhiJklMnoPqrStuVwx",
    "email": "volunteer.name@example.com",
    "short-password": "hunter2secret",
    # `AIza` + 35 of [A-Za-z0-9_-]: the real shape. Chosen because it is one
    # of the 4.8% that the catch-all does NOT stop on its own -- a key that
    # the catch-all happens to mask would leave this corpus proving nothing
    # about the rule that is supposed to be masking it.
    "google-api-key": "AIzaFAKE_fake_KEY_0123456789abcdefghijk",
}

SURVIVOR = "and this ordinary diagnostic sentence must survive intact"


def _planted_stderr() -> str:
    return "\n".join([
        f"Error: fetch failed for https://api.example.com/v1/chat"
        f"?key={PLANTED['openai-key']}&user=bob",
        f"  > Authorization: Bearer {PLANTED['jwt']}",
        f"  > x-api-key: {PLANTED['anthropic-key']}",
        f"curl -H 'Authorization: token {PLANTED['github-oauth']}' "
        f"https://api.github.com/user",
        f"GITHUB_TOKEN={PLANTED['github-pat']}",
        f"AWS_ACCESS_KEY_ID={PLANTED['aws-key-id']} "
        f"AWS_SECRET_ACCESS_KEY={PLANTED['aws-secret']}",
        f"contact {PLANTED['email']} to reset",
        "proxy: vmess://eyJhZGQiOiIxLjIuMy40In0= (subscription)",
        f"sub: https://sub.example.net/link/AbCdEf123456?token="
        f"{PLANTED['hex-digest']}",
        f"session={PLANTED['base64-padded']}",
        f"blob {PLANTED['base64-with-slash']}",
        f"codex session {PLANTED['fernet-session']}",
        f"digest {PLANTED['lowercase-hex']}",
        f"opaque {PLANTED['shaped-opaque']}",
        "refresh_token: 1//0gAbCdEfGhIjKlMnOpQrStUvWxYz",
        f"password={PLANTED['short-password']}",
        # Deliberately NOT written as `GOOGLE_API_KEY=<value>`: that field
        # name alone gets the value masked, which would let this corpus pass
        # while the redactor knew nothing about `AIza`. This is the shape
        # Google's own error actually prints.
        f"Error: API key not valid. Please pass a valid API key. "
        f"(key {PLANTED['google-api-key']})",
        SURVIVOR,
    ])


def _trial(tmp_path: Path, *, stderr: str | bytes | None,
           name: str = "antigravity.stderr.log") -> Path:
    trial = tmp_path / "trial"
    agent = trial / "agent"
    agent.mkdir(parents=True)
    # The boundary refuses a group/other-writable trial root, which an
    # inherited umask 002 would otherwise produce.
    trial.chmod(0o700)
    if stderr is not None:
        payload = stderr.encode() if isinstance(stderr, str) else stderr
        (agent / name).write_bytes(payload)
    return trial


# ---------------------------------------------------------------- redaction

def test_no_planted_credential_survives():
    """Negative control: nothing with a credential shape reaches the output."""
    redacted, labels = redact_diagnostic_text(_planted_stderr())

    leaked = [name for name, value in PLANTED.items() if value in redacted]
    assert leaked == []
    # ...and the redactor did not achieve that by deleting everything.
    assert SURVIVOR in redacted
    assert labels  # what was removed is reported, not silently dropped


def test_unrecognized_opaque_string_is_masked_by_default():
    """Default-deny: a run nobody has a pattern for is still treated as a
    credential. This is the policy that makes an unknown provider's unknown
    token format safe."""
    mystery = "Qx7vTnZr4KpLw9Bd2ScF"  # matches no known credential prefix
    redacted, labels = redact_diagnostic_text(f"emitted {mystery} then died")

    assert mystery not in redacted
    assert "[REDACTED-OPAQUE]" in redacted
    assert "OPAQUE" in labels
    assert "emitted" in redacted and "then died" in redacted


def test_real_error_text_is_not_damaged():
    """The point of the capture is reading the error, so the common shapes of
    a stack trace must pass through untouched."""
    text = (
        "NonZeroAgentExitCodeError: Command failed (exit 1)\n"
        "Error: The input token count (9571234) exceeds the maximum (1048576).\n"
        "    at GenerateContentStream.next "
        "(/opt/antigravity-runtime/bin/antigravity:184:29173)\n"
        "    at async ModelClient.streamGenerateContent (/app/src/x.js:1:2)\n"
        "Sha256HashMismatchError / UnhandledPromiseRejectionWarning\n"
        "FATAL: unhandled rejection, exiting with code 1\n"
    )
    redacted, labels = redact_diagnostic_text(text)

    assert redacted == text
    assert labels == []


@pytest.mark.parametrize("key", [
    PLANTED["google-api-key"],
    "AIzaSyD_fake-KEY_0123456789abcdefghijk",   # `_` and `-` throughout
    "AIzaSyB1234567890abcdefghijklmnopqrstuv",  # no separators at all
    "AIza" + "0123456789" * 3 + "abcde",        # digit-heavy tail
])
def test_google_api_key_is_masked(key):
    """`AIza` keys were reaching client_meta intact.

    `_` and `-` are outside the catch-all's run alphabet, so a real key is
    split into pieces that each clear _OPAQUE_MIN_CHARS on their own and the
    remainder often reads as an identifier (`0123456789abcdefghijk` decomposes
    into a digit run and a letter run). 4.8% of 5k keys generated to the real
    shape survived the whole pipeline before this rule existed.
    """
    redacted, labels = redact_diagnostic_text(f"agent emitted {key} then died")

    assert key not in redacted
    assert "[REDACTED-GOOGLE-API-KEY]" in redacted
    assert "GOOGLE-API-KEY" in labels


@pytest.mark.parametrize("shape,value", [
    ("google-api-key", PLANTED["google-api-key"]),
    ("github-oauth", PLANTED["github-oauth"]),
    ("jwt", PLANTED["jwt"]),
    ("proxy-uri", "vmess://eyJhZGQiOiIxLjIuMy40In0="),
])
@pytest.mark.parametrize("context", [
    "agent emitted {} then died",
    "  > blob {}",
    "config: {}",
])
def test_named_credential_shapes_are_masked_in_any_context(shape, value,
                                                           context):
    """The four shapes that reach this collector most often, each checked
    away from the field name that would otherwise catch it."""
    assert value not in redact_diagnostic_text(context.format(value))[0]


@pytest.mark.parametrize("text", [
    # Asserted on the rule's own label rather than on the text surviving:
    # the catch-all masks long hostname labels for an unrelated reason, and
    # this test must not quietly depend on that being fixed.
    "connect ECONNREFUSED generativelanguage.googleapis.com:443",
    "module AIzaHelper failed",     # fewer than 10 characters after `AIza`
    "AIzawa reported the fault",
    "aizaSyB1234567890abcdefghij",  # wrong case: not the Google prefix
])
def test_google_api_key_rule_does_not_fire_on_ordinary_text(text):
    redacted, labels = redact_diagnostic_text(text)

    assert "GOOGLE-API-KEY" not in labels
    assert "[REDACTED-GOOGLE-API-KEY]" not in redacted


# Everything in this block was masked before #0169. Each entry is content an
# operator needs in order to act on the capture, and the reason it was being
# eaten is recorded next to it: a run of >=16 payload characters was only
# spared when it split into two or more >=3-character segments, which no
# single word and no identifier built from two-letter words ever does.
UNMASKED_HOSTNAMES = [
    "generativelanguage.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "containerregistry.example.org",
    "securitytokenservice.example.net",
]
UNMASKED_WORDS = [
    "internationalization",   # ordinary prose
    "misconfiguration",
    "interoperability",
    "telecommunications",
    "djangorestframework",    # package names
    "pythonjsonlogger",
    "instrumentationtests",
]
UNMASKED_IDENTIFIERS = [
    "InterruptedIOException",       # `IO`
    "parseIntOrDefault",            # `Or`
    "getUserByIdOrThrow",           # `By`, `Id`, `Or`
    "createOrUpdateIfAbsent",       # `Or`, `If`
    "toStringAsFixedOrNull",        # `to`, `As`, `Or`
]


@pytest.mark.parametrize("hostname", UNMASKED_HOSTNAMES)
def test_hostname_survives_so_the_failing_endpoint_stays_readable(hostname):
    """The capture exists to say which endpoint failed. A host whose label
    is >=16 characters was being replaced with a placeholder, which removes
    exactly the part worth keeping."""
    text = f"connect ECONNREFUSED {hostname}:443"
    assert redact_diagnostic_text(text)[0] == text


@pytest.mark.parametrize("hostname", UNMASKED_HOSTNAMES)
def test_url_host_survives_the_catch_all_that_runs_after_the_url_rule(
        hostname):
    """_url_replacement deliberately keeps scheme/host/path. The catch-all
    runs afterwards over the whole string, so it used to undo that decision
    and take the `//` with it."""
    text = f"POST https://{hostname}/v1beta/models/x:stream failed"
    assert redact_diagnostic_text(text)[0] == text


@pytest.mark.parametrize("word", UNMASKED_WORDS)
def test_long_ordinary_words_are_not_credentials(word):
    text = f"error: {word} could not be resolved"
    assert redact_diagnostic_text(text)[0] == text


@pytest.mark.parametrize("identifier", UNMASKED_IDENTIFIERS)
def test_identifiers_built_from_two_letter_words_survive(identifier):
    """`Or`, `By`, `If`, `IO` are words, not the two-character case flips of
    a base62 payload -- see _SHORT_WORD_SEGMENTS."""
    text = f"{identifier}: operation failed"
    assert redact_diagnostic_text(text)[0] == text


def test_one_opaque_path_segment_does_not_condemn_the_whole_path():
    """`/` is inside the run alphabet, so a path is a single run. Masking
    the run whole threw away every other segment with it."""
    redacted, _ = redact_diagnostic_text(
        "open /var/lib/registry/Qx7vTnZr4KpLw9Bd2ScFmE/blobs failed"
    )
    assert redacted == (
        "open /var/lib/registry/[REDACTED-OPAQUE]/blobs failed"
    )


def test_base64_run_is_masked_whole_so_no_prefix_of_it_survives():
    """The counterpart to the test above. `+` and `=` are payload, not
    structure: a run carrying them is one credential, and masking it
    piecewise would publish its leading characters."""
    blob = "abc/def+ghiJKLmnoPQRstuVWXyz0123456789AB"
    redacted, _ = redact_diagnostic_text(f"blob {blob}")

    assert redacted == "blob [REDACTED-OPAQUE]"
    assert "abc/def" not in redacted


@pytest.mark.parametrize("digest", [
    PLANTED["lowercase-hex"],
    # The one above is also rejected by the letter-pair test, so on its own
    # it cannot tell us whether the hex-alphabet exclusion still works --
    # deleting that exclusion leaves the suite green. This one is pure a-f
    # AND has no unusual letter pair at all, so it reaches the allowance and
    # only the exclusion turns it back. 1.0% of random a-f runs look like
    # this; without the exclusion, that is the share of hex digests that
    # would be published as prose.
    "cafebabefacadebead",
])
def test_lowercase_hex_digest_is_not_mistaken_for_a_word(digest):
    """A hex digest is pure a-f and reads as flawless English -- `dead`,
    `beef`, `cafe`, `face` are words. Naming the alphabet is the only thing
    that rejects it."""
    redacted, labels = redact_diagnostic_text(f"digest {digest}")

    assert digest not in redacted
    assert "OPAQUE" in labels


@pytest.mark.parametrize("payload", [
    "qxvtnzrkplwbdscf",            # lowercase, no vowels
    "zxcvbnmasdfghjkl",            # lowercase, keyboard walk
    "Qx7vTnZr4KpLw9Bd2ScF",        # mixed case and digits
    # Vowel-balanced and free of consonant clusters: it defeats every
    # "does this look pronounceable" heuristic, and is caught only because
    # `bo`, `qi` and `xu` are not English letter pairs.
    "kaboqixuvenazirotemu",
])
def test_high_entropy_payload_is_still_masked_after_the_word_allowance(
        payload):
    """The allowance in _reads_as_english is for language, not for length."""
    redacted, labels = redact_diagnostic_text(f"emitted {payload} then died")

    assert payload not in redacted
    assert "OPAQUE" in labels


def test_planted_corpus_is_detectably_hostile(monkeypatch):
    """Negative control for the negative control.

    `test_no_planted_credential_survives` would pass just as happily if the
    corpus had stopped containing credentials, or if some unrelated stage
    were removing them. Neutralise every rule in this module and the same
    corpus has to come back carrying all of them -- otherwise that test is
    proving nothing about the redactor.
    """
    monkeypatch.setattr(agent_stderr, "_RULES", ())
    monkeypatch.setattr(agent_stderr, "scrub_text", lambda text: text)
    monkeypatch.setattr(agent_stderr, "_OPAQUE_RUN_RE", re.compile(r"(?!)"))

    redacted, labels = agent_stderr.redact_diagnostic_text(_planted_stderr())

    assert sorted(name for name, value in PLANTED.items()
                  if value in redacted) == sorted(PLANTED)
    assert labels == []


def test_url_keeps_endpoint_but_drops_query_and_userinfo():
    redacted, _ = redact_diagnostic_text(
        "POST https://user:pw@api.example.com/v1/chat?api_key=abc123&x=1 failed"
    )
    assert "https://[REDACTED-USERINFO]@api.example.com/v1/chat" in redacted
    assert "?[REDACTED-QUERY]" in redacted
    assert "api_key=abc123" not in redacted and "user:pw" not in redacted


def test_token_counts_are_not_mistaken_for_credentials():
    text = '{"token": 9571234, "n_input_tokens": 9571234}'
    assert redact_diagnostic_text(text)[0] == text


def test_placeholders_are_not_redacted_twice():
    """A second rule must not chew into a placeholder a first rule wrote and
    leave its closing bracket stranded."""
    redacted, _ = redact_diagnostic_text(
        f"  > x-api-key: {PLANTED['anthropic-key']}"
    )
    assert redacted == "  > x-api-key: [REDACTED-HEADER]"


def test_redaction_survives_undecodable_bytes(tmp_path):
    trial = _trial(tmp_path, stderr=b"sk-proj-abcdefghijklmnopqrst \x80\xff end")

    captured = collect_agent_stderr(trial)

    assert captured["agent_stderr_status"] == "captured"
    assert "sk-proj" not in captured["agent_stderr_tail"]
    assert "end" in captured["agent_stderr_tail"]


# --------------------------------------------------------------- collection

def test_collects_tail_only_and_reports_truncation(tmp_path):
    body = "".join(f"line {index} of agent noise\n" for index in range(20000))
    stderr = "FIRST MARKER\n" + body + "LAST MARKER: boom\n"
    trial = _trial(tmp_path, stderr=stderr)

    captured = collect_agent_stderr(trial)

    assert captured["agent_stderr_status"] == "captured"
    assert captured["agent_stderr_bytes"] == len(stderr.encode())
    assert captured["agent_stderr_truncated"] is True
    assert "LAST MARKER: boom" in captured["agent_stderr_tail"]
    assert "FIRST MARKER" not in captured["agent_stderr_tail"]
    assert len(captured["agent_stderr_tail"]) <= MAX_REDACTED_CHARS


def test_only_the_final_bytes_are_read_into_the_tail(tmp_path):
    """The cap is a byte window on the log, not just a cap on the redacted
    string: everything older than that window must never be loaded into
    client_meta at all."""
    stderr = "".join(f"line{index:04d} padding padding\n" for index in range(400))
    trial = _trial(tmp_path, stderr=stderr)

    captured = collect_agent_stderr(trial, tail_bytes=200)

    tail = captured["agent_stderr_tail"]
    assert captured["agent_stderr_truncated"] is True
    assert captured["agent_stderr_bytes"] == len(stderr.encode())
    assert len(tail.encode()) <= 200
    assert "line0399" in tail
    assert "line0300" not in tail


def test_small_stderr_is_not_marked_truncated(tmp_path):
    trial = _trial(tmp_path, stderr="boom\n")

    captured = collect_agent_stderr(trial)

    assert captured["agent_stderr_truncated"] is False
    assert captured["agent_stderr_tail"].strip() == "boom"
    assert captured["agent_stderr_source"] == "antigravity.stderr.log"


def test_empty_stderr_is_reported_as_empty(tmp_path):
    trial = _trial(tmp_path, stderr="")

    captured = collect_agent_stderr(trial)

    assert captured["agent_stderr_status"] == "empty"
    assert "agent_stderr_tail" not in captured


@pytest.mark.parametrize("name", STDERR_CANDIDATES)
def test_every_known_adapter_log_name_is_collected(tmp_path, name):
    trial = _trial(tmp_path, stderr="boom\n", name=name)

    captured = collect_agent_stderr(trial)

    assert captured["agent_stderr_status"] == "captured"
    assert captured["agent_stderr_source"] == name


def test_candidate_list_covers_every_adapter_stderr_file():
    """Drift guard. The adapters cannot be imported here (the `pier` runtime
    is not a test dependency), so their source is read instead."""
    adapters = (Path(__file__).resolve().parents[1] / "src" / "dradar")
    declared = {
        match.group(1)
        for path in adapters.glob("pier_*.py")
        for match in re.finditer(
            r"""_STDERR_FILE\s*=\s*["']([^"']+)["']""", path.read_text()
        )
    }
    assert declared, "expected at least one adapter to declare _STDERR_FILE"
    assert declared <= set(STDERR_CANDIDATES), (
        f"adapter stderr logs missing from STDERR_CANDIDATES: "
        f"{sorted(declared - set(STDERR_CANDIDATES))}"
    )


# ------------------------------------------------- failure must stay bounded

def test_missing_stderr_artifact_is_reported_not_raised(tmp_path):
    trial = _trial(tmp_path, stderr=None)

    captured = collect_agent_stderr(trial)

    assert captured["agent_stderr_status"] == "unavailable"
    assert captured["agent_stderr_unavailable_reason"] == "no_stderr_artifact"


def test_missing_trial_directory_is_reported_not_raised(tmp_path):
    captured = collect_agent_stderr(tmp_path / "never-created")

    assert captured["agent_stderr_status"] == "unavailable"
    assert captured["agent_stderr_unavailable_reason"] == "trial_files_missing"


def test_symlinked_stderr_is_refused_by_the_artifact_boundary(tmp_path):
    """The container writes this file; a symlink out of the trial must not be
    followed into the volunteer's own credentials."""
    secret = tmp_path / "outside.txt"
    secret.write_text("sk-proj-abcdefghijklmnopqrstuvwxyz012345\n")
    trial = _trial(tmp_path, stderr=None)
    (trial / "agent" / "antigravity.stderr.log").symlink_to(secret)

    captured = collect_agent_stderr(trial)

    assert captured["agent_stderr_status"] == "unavailable"
    assert captured["agent_stderr_unavailable_reason"].startswith("boundary_")
    assert "sk-proj" not in json.dumps(captured)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_unreadable_stderr_is_reported_not_raised(tmp_path):
    trial = _trial(tmp_path, stderr="boom\n")
    (trial / "agent" / "antigravity.stderr.log").chmod(0o000)

    captured = collect_agent_stderr(trial)

    assert captured["agent_stderr_status"] == "unavailable"
    assert captured["agent_stderr_unavailable_reason"].startswith("errno_")


def test_oversized_stderr_is_refused_without_reading_it_all(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_stderr, "READ_LIMIT_BYTES", 1024)
    trial = _trial(tmp_path, stderr="x" * 4096)

    captured = collect_agent_stderr(trial)

    assert captured["agent_stderr_status"] == "unavailable"
    assert captured["agent_stderr_unavailable_reason"] == "boundary_file_limit"


# ------------------------------------------------------------ when to collect

def test_only_a_non_zero_agent_exit_triggers_collection(tmp_path):
    trial = _trial(tmp_path, stderr="boom\n")
    result = trial / "result.json"

    result.write_text(json.dumps(COMPLETED))
    assert collect_for_agent_exit(trial, result) == {}

    result.write_text(json.dumps(NON_ZERO_EXIT))
    assert collect_for_agent_exit(trial, result)["agent_stderr_status"] == "captured"


@pytest.mark.parametrize("payload", ["not json at all", "[]", '{"exception_info": 3}'])
def test_unusable_result_json_never_raises(tmp_path, payload):
    trial = _trial(tmp_path, stderr="boom\n")
    result = trial / "result.json"
    result.write_text(payload)

    assert agent_exited_non_zero(result) is False
    assert collect_for_agent_exit(trial, result) == {}


def test_absent_result_json_never_raises(tmp_path):
    trial = _trial(tmp_path, stderr="boom\n")

    assert agent_exited_non_zero(None) is False
    assert collect_for_agent_exit(trial, trial / "result.json") == {}


# ------------------------------------------------------- end to end, via go

def _run(monkeypatch, tmp_path, *, result_data, stderr):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    art = _fake_art(tmp_path, rc=0, result_data=result_data)
    if stderr is not None:
        agent = art.trial_dir / "agent"
        agent.mkdir(parents=True, exist_ok=True)
        (agent / "antigravity.stderr.log").write_text(stderr)
    art.trial_dir.chmod(0o700)
    monkeypatch.setattr(runloop, "run_trial", lambda *a, **kw: art)
    client = SubmitClient({})
    runloop._run_and_submit(client, ASSIGNMENT, tmp_path, _args(), "abc123")
    return client.submissions[0]["meta"]


def test_failed_run_uploads_a_redacted_stderr_tail(monkeypatch, tmp_path):
    meta = _run(
        monkeypatch, tmp_path,
        result_data=NON_ZERO_EXIT, stderr=_planted_stderr(),
    )

    assert meta["agent_stderr_status"] == "captured"
    assert SURVIVOR in meta["agent_stderr_tail"]
    serialized = json.dumps(meta)
    assert [name for name, value in PLANTED.items() if value in serialized] == []


def test_completed_run_uploads_no_stderr_fields(monkeypatch, tmp_path):
    meta = _run(monkeypatch, tmp_path, result_data=COMPLETED, stderr="boom\n")

    assert [key for key in meta if key.startswith("agent_stderr")] == []


def test_capture_failure_does_not_block_the_submission(monkeypatch, tmp_path):
    meta = _run(monkeypatch, tmp_path, result_data=NON_ZERO_EXIT, stderr=None)

    assert meta["agent_stderr_status"] == "unavailable"
    assert meta["agent_stderr_unavailable_reason"] == "no_stderr_artifact"
    # The run's real evidence is untouched by the diagnostic capture failing.
    assert meta["exception_type"] == "NonZeroAgentExitCodeError"


def test_collection_is_ordered_before_container_teardown():
    """`cleanup_trial_resources` removes this trial's containers and volumes.
    The capture has to be upstream of it in _run_and_submit, not merely
    'usually fine because agent/ is a host bind mount'."""
    source = (Path(runloop.__file__)).read_text()
    collect = source.index("agent_stderr.collect_for_agent_exit(")
    teardown = source.index("image_cache.cleanup_trial_resources(")

    assert collect < teardown
