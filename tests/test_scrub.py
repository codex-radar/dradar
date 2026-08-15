import hashlib
import json

from dradar.scrub import (
    patch_structure_is_valid, redact_patch_secrets, scan_secrets, scrub_bytes,
    scrub_json_bytes, scrub_text,
)


def test_scrubs_openai_key():
    assert "sk-" not in scrub_text("key=sk-proj-abc123def456ghi789jkl000")


def test_scrubs_anthropic_key_with_correct_label():
    out = scrub_text("token: sk-ant-api03-xxxxxxxxxxxxxxxxxxxxx")
    assert "sk-ant-api03" not in out
    assert "[REDACTED-SK-ANT]" in out  # not mislabeled as generic SK


def test_scrubs_email_and_home():
    out = scrub_text("aloha@example.com wrote /Users/aloha/x and /home/bob/y")
    assert "aloha@example.com" not in out
    assert "/Users/aloha" not in out and "/home/bob" not in out
    assert "/[HOME]/x" in out


def test_scrubs_opaque_fernet_token():
    tok = "gAAAAABm" + "Zk9" * 20
    assert tok not in scrub_text(f"session={tok}")


def test_keeps_normal_code():
    code = "def apply(x):\n    return x + 1  # normal comment\n"
    assert scrub_text(code) == code


def test_scrub_bytes_never_bypasses_on_bad_utf8():
    # A secret next to an invalid UTF-8 byte must still be redacted (not
    # written verbatim as the old UnicodeDecodeError fallback did).
    data = b"sk-proj-abc123def456ghi789xyz \x80\xff tail"
    out = scrub_bytes(data)
    assert b"sk-proj" not in out
    assert b"\x80\xff" in out  # non-secret bytes round-trip intact


def test_scrub_json_bytes_preserves_escaped_quotes_and_redacts_values():
    token = "abcdefghijklmnop"
    payload = {
        "authorization": token + '"suffix',
        "nested": [{"message": "authorization:" + token + '"suffix'}],
        "contact": "aloha@example.com at /Users/aloha/project",
    }

    out = scrub_json_bytes(json.dumps(payload).encode())
    decoded = json.loads(out)

    assert decoded["authorization"] == "[REDACTED-AUTH]"
    assert decoded["nested"][0]["message"].endswith('"suffix')
    assert token not in decoded["nested"][0]["message"]
    assert "aloha@example.com" not in decoded["contact"]
    assert "/Users/aloha" not in decoded["contact"]


def test_scrub_json_bytes_handles_sensitive_keys_inside_nested_objects():
    payload = {
        "headers": {"Proxy-Authorization": "opaque-value", "ok": True},
        "credentials": [{"api_key": "not-a-real-key-12345"}],
    }

    decoded = json.loads(scrub_json_bytes(json.dumps(payload).encode()))

    assert decoded["headers"]["Proxy-Authorization"] == "[REDACTED-AUTH]"
    assert decoded["headers"]["ok"] is True
    assert decoded["credentials"][0]["api_key"] == "[REDACTED]"


def test_scrub_json_bytes_replaces_inline_images_with_auditable_marker():
    encoded = "aGVsbG8="
    data_url = "data:image/png;base64," + encoded
    payload = {
        "direct": {"image_url": data_url},
        # Pier can wrap a tool result in a JSON-encoded string. The compactor
        # must catch that representation without changing its outer type.
        "wrapped": json.dumps({"type": "input_image", "image_url": data_url}),
    }

    decoded = json.loads(scrub_json_bytes(json.dumps(payload).encode()))
    digest = hashlib.sha256(encoded.encode("ascii")).hexdigest()
    marker = (
        "[DRADAR-OMITTED-INLINE-IMAGE media_type=image/png "
        f"encoded_chars={len(encoded)} base64_sha256={digest}]"
    )
    assert decoded["direct"]["image_url"] == marker
    assert marker in decoded["wrapped"]
    assert "aGVsbG8=" not in json.dumps(decoded)


def test_scan_secrets_detects_without_rewriting():
    assert scan_secrets(b"+ api_key = sk-proj-abc123def456ghi789xyz\n")
    assert scan_secrets(b"ghp_" + b"a" * 30)
    assert scan_secrets(b"eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NQ.abcdefghijklmnop")


def test_scan_secrets_clean_patch_passes():
    patch = b"diff --git a/x b/x\n@@ -1 +1 @@\n-old value\n+new value\n"
    assert scan_secrets(patch) == []


def test_patch_redaction_only_rewrites_added_hunk_lines():
    patch = b"""diff --git a/app.py b/app.py
index 3367afd..f04ce62 100644
--- a/app.py
+++ b/app.py
@@ -1 +1,2 @@
 old = True
+api_key = \"ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456\"
"""
    redacted, labels, unsafe = redact_patch_secrets(patch)
    assert "GHP" in labels and unsafe == []
    assert b"ghp_" not in redacted
    assert b'api_key = "[REDACTED-GHP]"' in redacted
    assert scan_secrets(redacted) == []
    assert patch_structure_is_valid(redacted)


def test_patch_redaction_rejects_secret_in_context_line():
    patch = b"""diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1,2 @@
 ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456
+safe = True
"""
    redacted, labels, unsafe = redact_patch_secrets(patch)
    assert labels == [] and "GHP" in unsafe
    assert b"ghp_" in redacted
