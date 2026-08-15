"""Secret handling for uploads.

Two artifact classes, two mechanisms (design doc):

- **Integrity-critical** (model.patch): detect secrets first. A secret may be
  redacted only from an added line inside a unified-diff hunk; added content
  is not used to locate the hunk, so context/deletion applicability stays
  unchanged. Hits anywhere else remain quarantined. The sanitized copy must
  parse as a patch and pass a second secret scan before upload. The raw patch
  is never rewritten.

- **Display** (trajectory.json, result.json): shown in the public viewer, so
  redact destructively. JSON artifacts are parsed and scrubbed structurally;
  applying regexes to their serialized bytes can consume JSON escape
  backslashes and corrupt otherwise valid data. Over-redaction is acceptable
  here.

Both run client-side before upload AND server-side before storage. Neither
path ever bypasses on non-UTF-8 input: bytes are decoded with
``surrogateescape`` so arbitrary bytes round-trip while ASCII-shaped secrets
are still caught.
"""

import hashlib
import json
import re
import subprocess
from pathlib import Path

# High-precision credential shapes — used to DETECT secrets in a patch. Kept
# conservative: every entry matches something that is unambiguously a
# credential, never ordinary source content, so scan_secrets does not falsely
# reject a valid patch. (label, pattern)
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("SK-ANT", re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}")),  # before generic sk-
    ("SK", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{16,}")),
    ("GHP", re.compile(r"ghp_[A-Za-z0-9]{20,}")),
    ("GH-PAT", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("JWT", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    # ChatGPT/Codex OAuth session blobs (Fernet-style, opaque, no key= label).
    ("FERNET", re.compile(r"gAAAAA[A-Za-z0-9_-]{40,}")),
    ("BEARER", re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]{20,}=*")),
    ("AUTH-HEADER", re.compile(r"(?i)authorization[\"']?\s*[:=]\s*[\"']?[^\s\"']{12,}")),
    ("KEY-ASSIGN", re.compile(
        r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|secret)"
        r"[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9._~+/-]{16,}=*"
    )),
]

# Credential-only rewrites. These may be applied to added patch lines and are
# also a subset of the more aggressive display-artifact scrubber below.
_SECRET_SCRUB_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}"), "[REDACTED-SK-ANT]"),
    (re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{16,}"), "[REDACTED-SK]"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "[REDACTED-GHP]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "[REDACTED-GH-PAT]"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "[REDACTED-JWT]"),
    (re.compile(r"gAAAAA[A-Za-z0-9_-]{40,}"), "[REDACTED-TOKEN]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]{20,}=*"), r"\1[REDACTED-BEARER]"),
    (re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?)[^\s\"']{12,}"), r"\1[REDACTED-AUTH]"),
    (re.compile(
        r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|secret)"
        r"[\"']?\s*[:=]\s*[\"']?)[A-Za-z0-9._~+/-]{16,}=*"
    ), r"\1[REDACTED]"),
]

# Destructive rewrites for DISPLAY artifacts only. PII is safe to redact in a
# trajectory but must not be rewritten in executable source patches.
_SCRUB_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    *_SECRET_SCRUB_PATTERNS,
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[REDACTED-EMAIL]"),
]

# Home-dir paths reveal local usernames; container paths (/app, /logs) are fine.
_HOME_RE = re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+")

# The text scrubber recognizes values written next to these field names. When
# JSON is parsed first, key and value are separate strings, so retain that
# protection explicitly instead of relying on serialized punctuation.
_SENSITIVE_JSON_KEY_RE = re.compile(
    r"(?i)(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|secret)$"
)
_AUTH_JSON_KEY_RE = re.compile(r"(?i)authorization$")
_OPAQUE_AUTH_VALUE_RE = re.compile(r"[^\s\"']{12,}")
_OPAQUE_SECRET_VALUE_RE = re.compile(r"[A-Za-z0-9._~+/-]{16,}=*")

# Codex computer/browser tool outputs may embed screenshots as multi-megabyte
# data URLs. Their pixels are not needed for grading, usage accounting, or
# web-tool audit, and retaining them duplicates the same image in the legacy
# trajectory and the multi-agent bundle. Preserve tamper-evident metadata
# instead of uploading the raw display payload.
_INLINE_IMAGE_DATA_RE = re.compile(
    r"data:(image/[A-Za-z0-9.+-]+);base64,([A-Za-z0-9+/_=-]+)",
    re.IGNORECASE,
)


def _decode(data: bytes) -> str:
    # surrogateescape round-trips arbitrary bytes: no scrub/scan bypass on
    # non-UTF-8 input, and re-encoding reproduces the original bytes exactly.
    return data.decode("utf-8", errors="surrogateescape")


def scan_secrets(data: bytes) -> list[str]:
    """Return the labels of any credential shapes found. Non-empty => the
    caller (patch handler) should reject/quarantine rather than store."""
    text = _decode(data)
    return [label for label, pat in _SECRET_PATTERNS if pat.search(text)]


def redact_patch_secrets(data: bytes) -> tuple[bytes, list[str], list[str]]:
    """Redact credentials only from added unified-diff hunk lines.

    Returns ``(redacted_bytes, redacted_labels, unsafe_labels)``. Any label in
    ``unsafe_labels`` means a credential occurred in metadata, context, or a
    deletion line; rewriting it could change hunk matching, so callers must
    quarantine instead of uploading. Arbitrary non-UTF-8 bytes round-trip.
    """
    in_hunk = False
    output: list[str] = []
    redacted: set[str] = set()
    unsafe: set[str] = set()
    for line in _decode(data).splitlines(keepends=True):
        if line.startswith("diff --git "):
            in_hunk = False
        elif line.startswith("@@"):
            in_hunk = True
        hits = {label for label, pattern in _SECRET_PATTERNS if pattern.search(line)}
        if hits:
            if in_hunk and line.startswith("+"):
                for pattern, replacement in _SECRET_SCRUB_PATTERNS:
                    line = pattern.sub(replacement, line)
                redacted.update(hits)
            else:
                unsafe.update(hits)
        output.append(line)
    encoded = "".join(output).encode("utf-8", errors="surrogateescape")
    return encoded, sorted(redacted), sorted(unsafe)


def patch_structure_is_valid(data: bytes) -> bool:
    """Ask git to parse a diff without needing or modifying a worktree."""
    try:
        proc = subprocess.run(
            ["git", "apply", "--numstat", "-"], input=data,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return False
    return proc.returncode == 0


def scrub_text(text: str) -> str:
    for pat, repl in _SCRUB_PATTERNS:
        text = pat.sub(repl, text)
    return _HOME_RE.sub("/[HOME]", text)


def scrub_bytes(data: bytes) -> bytes:
    """Destructively redact a display artifact. Never bypasses: arbitrary
    bytes round-trip via surrogateescape while ASCII secrets are redacted."""
    return scrub_text(_decode(data)).encode("utf-8", errors="surrogateescape")


def _compact_inline_images(text: str) -> str:
    def marker(match: re.Match[str]) -> str:
        media_type = match.group(1).lower()
        payload = match.group(2)
        digest = hashlib.sha256(payload.encode("ascii")).hexdigest()
        return (
            "[DRADAR-OMITTED-INLINE-IMAGE "
            f"media_type={media_type} encoded_chars={len(payload)} "
            f"base64_sha256={digest}]"
        )

    return _INLINE_IMAGE_DATA_RE.sub(marker, text)


def _scrub_json_value(value: object) -> object:
    if isinstance(value, str):
        # Compact first: credential regexes should not scan megabytes of image
        # encoding, and screenshots can visually contain data that text-only
        # redaction cannot reliably detect.
        return scrub_text(_compact_inline_images(value))
    if isinstance(value, list):
        return [_scrub_json_value(item) for item in value]
    if isinstance(value, dict):
        scrubbed: dict[str, object] = {}
        for key, item in value.items():
            # json.loads always produces string object keys.
            clean_key = scrub_text(key)
            is_auth = bool(_AUTH_JSON_KEY_RE.search(key))
            is_sensitive = bool(_SENSITIVE_JSON_KEY_RE.search(key))
            should_redact = isinstance(item, str) and (
                (is_auth and _OPAQUE_AUTH_VALUE_RE.match(item) is not None)
                or (is_sensitive and not is_auth
                    and _OPAQUE_SECRET_VALUE_RE.match(item) is not None)
            )
            if should_redact:
                scrubbed[clean_key] = ("[REDACTED-AUTH]"
                                       if is_auth else "[REDACTED]")
            else:
                scrubbed[clean_key] = _scrub_json_value(item)
        return scrubbed
    return value


def scrub_json_bytes(data: bytes) -> bytes:
    """Redact a JSON artifact without editing its serialized syntax.

    Parsing before redaction keeps escape backslashes out of the regex input;
    re-serialization therefore guarantees that valid input remains valid JSON.
    Invalid JSON raises instead of being uploaded or bypassing redaction.
    """
    value = json.loads(data)
    scrubbed = _scrub_json_value(value)
    serialized = json.dumps(
        scrubbed, ensure_ascii=False, separators=(",", ":"),
    )
    try:
        return serialized.encode("utf-8")
    except UnicodeEncodeError:
        # JSON may legally contain an escaped unpaired surrogate. Keep the
        # output valid and portable without rejecting the whole artifact.
        return json.dumps(
            scrubbed, ensure_ascii=True, separators=(",", ":"),
        ).encode("ascii")


def scrub_file(source: Path, target: Path) -> None:
    """Scrub a display artifact from source into target (target dir must exist)."""
    target.write_bytes(scrub_bytes(source.read_bytes()))
