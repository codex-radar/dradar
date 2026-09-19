"""Preserve the provider agent's own stderr tail when it exits non-zero.

Pier records a failed agent command as ``NonZeroAgentExitCodeError`` carrying
the exit status and the command line.  It does not record *why* the agent
died: that text goes to the agent's own stderr inside the task container
(``/logs/agent/<agent>.stderr.log``).  Without it a whole class of provider
failure can only ever be reported as "exit 1" plus inference — which is
exactly where the gemini-3.8-flash investigation stopped.

Two constraints shape everything below.

**The file is written by the untrusted container.**  It is read through
``artifact_boundary`` like every other trial artifact, never with a plain
``open()``, and only a bounded tail is kept.

**Its contents are assumed hostile to redaction.**  Agent stderr can carry
OAuth tokens, API keys, proxy subscription URLs, request headers and mail
addresses, and this text is bound for ``client_meta`` — the submissions table
and the server's logs.  The redactor therefore *fails toward masking*: known
credential shapes are rewritten by label, and anything else long and opaque
enough to be a credential is masked even though it was not recognized.
Losing a stack frame is cheap; writing a live token into the database is not.

**What this does not catch.**  Masking is not a proof, and reading it as one
is the way a credential eventually ships.  These bounds are deliberate:

- The catch-all fires at ``_OPAQUE_MIN_CHARS`` (16).  A credential shorter
  than that, with no recognized prefix, not adjacent to a credential field
  name and not inside a URL query, is passed through.  The floor matches
  scrub.py's own ``{16,}`` notion of credential length; lowering it starts
  eating ordinary words out of the error text this exists to preserve.
- Runs that decompose into real words are kept, so the exception class names
  worth reading survive.  A payload shaped exactly like an identifier would
  survive with them.  Random base62 does not decompose that way, which is
  what makes the allowance affordable, not a proof that nothing can.
- Runs that read as one English word are kept too, so that the hostname of
  the endpoint that failed survives -- see ``_reads_as_english``, which
  states its own measured cost.

And one gap that is not deliberate, recorded here because the comment below
used to claim the opposite: a base64 run broken by ``/`` into pieces that
are each under ``_OPAQUE_MIN_CHARS`` is passed through whole, because every
piece clears the length floor on its own.  Splitting on ``/`` bounds how
much of a path is lost, not how much of a blob is kept.

Widen a rule before assuming a shape is covered, and keep the negative
control in tests/test_agent_stderr.py as the thing that decides.

Nothing here may raise.  A failed capture degrades to a recorded reason; the
submission it belongs to must still complete.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

from .artifact_boundary import TrialFiles, UnsafeArtifact
from .scrub import scrub_text

# Only the tail is evidence: the interesting output of a crashed agent is
# what it printed last.  client_meta is subtracted from the server's whole
# submission body budget (MAX_SUBMISSION_CONTENT_BYTES), so this stays small
# enough to be noise against the patch and trajectory.
TAIL_BYTES = 16 * 1024
# Redaction can grow text (a 16-char run becomes a 17-char placeholder), so
# the produced string is capped independently of the byte tail.
MAX_REDACTED_CHARS = 24 * 1024
# A runaway agent can produce a very large log.  Reading is bounded well
# below artifact_boundary's own 64 MiB ceiling because the whole file is
# materialized to reach its tail.
READ_LIMIT_BYTES = 8 * 1024 * 1024

# Pier adapters write their agent's stderr to a fixed name under the trial's
# `agent/` directory (the host side of the container's /logs/agent bind
# mount).  A new adapter with a new _STDERR_FILE must be added here;
# tests/test_agent_stderr.py fails if one is missing.
STDERR_CANDIDATES = (
    "antigravity.stderr.log",
    "kimi-code.stderr.log",
    "zcode-stderr.log",
)

_AGENT_LOG_DIR = "agent"
_RESULT_READ_LIMIT_BYTES = 8 * 1024 * 1024

STATUS_CAPTURED = "captured"
STATUS_EMPTY = "empty"
STATUS_UNAVAILABLE = "unavailable"


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------

# Proxy/node subscription URIs are credentials in their entirety — the
# userinfo, the host and the fragment all identify a paid node.
_PROXY_URI_RE = re.compile(
    r"(?i)\b(ss|ssr|vmess|vless|trojan|trojan-go|hysteria2?|hy2|tuic|snell|"
    r"juicity|naive\+?[a-z]*|wireguard)://\S+"
)

# Ordinary URLs keep scheme/host/path (which endpoint failed is the useful
# part) and lose userinfo, query and fragment (which is where tokens ride).
_URL_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*)://([^\s\"'<>\\]+)")

# Header dumps, whether one per line or inline in a curl echo / dict repr.
_HEADER_RE = re.compile(
    r"(?i)\b(authorization|proxy-authorization|www-authenticate|cookie|"
    r"set-cookie|x-api-key|api-key|apikey|x-auth-token|x-session-token|"
    r"x-goog-api-key|x-goog-iam-authorization-token)\b"
    r"([\"']?\s*[:=]\s*)"
    r"(\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\r\n,;}\]\"']+)"
)

_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+([A-Za-z0-9._~+/=-]{6,})")

# Unambiguous credential field names: any value at all is a secret.
_STRONG_ASSIGNMENT_RE = re.compile(
    r"(?i)\b((?:api[-_]?key|access[-_]?token|refresh[-_]?token|id[-_]?token|"
    r"auth[-_]?token|session[-_]?token|client[-_]?secret|private[-_]?key|"
    r"secret|password|passwd|pwd|credential)s?"
    r"[\"']?\s*[:=]\s*[\"']?)([^\s\"',;}\]]{4,})"
)
# Ambiguous names that also appear in ordinary diagnostics ("token count",
# "auth failed").  Require a credential-length value so error prose survives;
# anything shorter that is still opaque is caught by the catch-all below.
_WEAK_ASSIGNMENT_RE = re.compile(
    r"(?i)\b((?:token|auth|signature|sig|session)"
    r"[\"']?\s*[:=]\s*[\"']?)([^\s\"',;}\]]{12,})"
)

# scrub.py knows ghp_/github_pat_; the OAuth, server, user and refresh
# variants leak the same way.
_GITHUB_TOKEN_RE = re.compile(r"\bgh[opsur]_[A-Za-z0-9]{8,}")

# Google API keys are `AIza` followed by 35 characters of [A-Za-z0-9_-].
# Neither scrub.py nor anything above knows that shape, and `_` and `-` are
# outside the catch-all's run alphabet, so a real key is chopped into pieces
# that each clear the length floor on their own -- measured over 5k keys
# generated to the real shape, 4.8% came through the whole pipeline intact.
# The tail after `AIza` is required to be at least 10 characters so that the
# rule keys on a credential rather than on any word beginning `AIza`.
_GOOGLE_API_KEY_RE = re.compile(r"\bAIza[A-Za-z0-9_-]{10,}")

# The catch-all.  A "run" is a maximal stretch of base64/base64url payload
# characters; it is masked unless every one of its `+/=`-separated segments
# is demonstrably not a credential.  Splitting on those separators is what
# keeps filesystem paths readable.  It does NOT stop a base64 blob that
# happens to contain `/` from slipping through in short pieces -- see the
# last paragraph of the module docstring.
_OPAQUE_MIN_CHARS = 16
_OPAQUE_MAX_WORD_CHARS = 40
# Below this length a lowercase run has to be *clean* English, not merely
# close to it.  See _reads_as_english for the measurement that set it.
_OPAQUE_STRICT_WORD_CHARS = 18
_OPAQUE_RUN_RE = re.compile(r"[A-Za-z0-9+/=]{%d,}" % _OPAQUE_MIN_CHARS)
_OPAQUE_SPLIT_RE = re.compile(r"[+/=]+")
# Same split, but keeping the separators, for the path-shaped runs that are
# masked segment by segment rather than whole.  See _opaque_replacement.
_OPAQUE_PATH_SPLIT_RE = re.compile(r"(/+)")
# CamelCase / snake-free identifier segmentation: real identifiers decompose
# into words, random base62 decomposes into one-character case flips.
_IDENT_SEGMENT_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+|[0-9]+")

# Two-letter English words, plus `io`.  Real identifiers are built from real
# words, and some of those words are two letters long — which is why
# `InterruptedIOException`, `parseIntOrDefault` and `createOrUpdateIfAbsent`
# were being masked as payloads.  Requiring every short segment to come from
# this closed list is what still separates them from a base62 blob's
# arbitrary two-character case flips: over 20k random runs of each shape,
# admitting them moves the leak rate by 0.000pp (base62) and 0.025pp
# (mixed-case letters only), and it recovers every identifier of that shape.
# The list is closed on purpose — relaxing it to *any* two-character segment
# is precisely the hole it exists to prevent.
_SHORT_WORD_SEGMENTS = frozenset(
    "am an as at be by do go he id if in io is it me my no of ok on or so "
    "to up us we".split()
)

# Letter pairs occurring at least 200 times across /usr/share/dict/words.  A
# lowercase run is only read as language when nearly every adjacent pair is
# one of these; see _reads_as_english for what that buys and what it costs.
_ENGLISH_PAIR_SOURCE = (
    "abacadaeafagahaiakalamanaoaparasatauavawaxayazbabbbcbdbebiblbobrbs"
    "btbubycacccechcickclcocrcsctcucydadddedfdgdhdidldmdndodrdsdudvdwdy"
    "eaebecedeeefegeheiejekelemeneoepeqereseteuevewexeyezfafefffiflfofr"
    "ftfufygageggghgiglgmgngogrgsgugyhahehihlhmhnhohrhthuhwhyiaibicidie"
    "ifigihiiikiliminioipiqirisitiuivixizjajejijojukakekhkiklknkokrksku"
    "kwkylalblcldlelflglilklllmlnlolplsltlulvlwlymambmemimlmmmnmompmsmu"
    "mynanbncndnenfngnhninjnknlnmnnnonpnqnrnsntnunvnwnynzoaobocodoeofog"
    "ohoiokolomonooopoqorosotouovowoxoyozpapephpiplpnpoppprpsptpupyqura"
    "rbrcrdrerfrgrhrirkrlrmrnrorprrrsrtrurvrwrysasbscsesfshsiskslsmsnso"
    "spsqssstsuswsytatbtctetfthtitltmtntotptrtstttutwtyuaubucudueufugui"
    "ukulumunuoupurusutuvuxvavevivovuwawewhwiwlwnwowrwsxaxcxexixoxpxtxy"
    "yaybycydyeygyiylymynyoypyrysytywzazezizozyzz"
)
_ENGLISH_PAIRS = frozenset(
    _ENGLISH_PAIR_SOURCE[index:index + 2]
    for index in range(0, len(_ENGLISH_PAIR_SOURCE), 2)
)


def _looks_like_identifier(segment: str) -> bool:
    """True for `NonZeroAgentExitCodeError`, false for `4eC39HqLyjWDarjtT1`.

    A source identifier decomposes into real words; a base62 payload
    decomposes into short case/digit flips.  Every condition below is load
    bearing: dropping any one of them lets a hand-shaped blob such as
    `ghiJKLmnoPQRstuVWXyz0123456789AB` read as an identifier.
    """
    segments = _IDENT_SEGMENT_RE.findall(segment)
    if "".join(segments) != segment or len(segments) < 2:
        return False
    if any(len(part) < 3 and part.lower() not in _SHORT_WORD_SEGMENTS
           for part in segments):
        return False
    if max(len(part) for part in segments) < 4:
        return False
    # `Sha256HashMismatchError` is an identifier; scattered digits are entropy.
    return sum(1 for part in segments if part.isdigit()) <= 1


def _reads_as_english(segment: str) -> bool:
    """True for `generativelanguage`, false for `qxvtnzrkplwbdscf`.

    An undivided run gives `_looks_like_identifier` nothing to work with —
    it needs two segments — so every long lowercase word was masked:
    hostname labels (`generativelanguage.googleapis.com`), package names
    (`djangorestframework`), ordinary prose (`misconfiguration`).  That is
    the opposite of what this module is for.  A stderr line that no longer
    names the endpoint that failed cannot locate the failure.

    What actually holds the line, and what each part is worth:

    - The letter pairs must be English ones.  That is the whole test; the
      numbers below are its numbers.
    - Lowercase hex digests are excluded by naming their alphabet.
      `deadbeefcafebabe...` is pure a-f and reads as flawless English by
      every statistical test tried here, so nothing else rejects it, and
      tests/test_agent_stderr.py plants exactly that string.  The price of
      that exclusion is that git SHAs, `sha256:` digests, hyphenless UUIDs
      and container ids stay masked.  Those are ops evidence and losing
      them is a real cost, accepted here to keep digests masked.
    - The `isascii/isalpha/islower` precondition is *not* an independent
      bound.  At credential lengths the pair test already rejects
      everything it rejects.  It earns its place only on the
      `_is_recognizable_segment` path, which has no length floor, where it
      stops a lone uppercase character from counting as a word.  Do not
      cite it as a third barrier.

    **The price, stated at the length where it is highest.**  Over 40k
    uniformly random lowercase runs of length 16-44, 0.11% pass, but that
    is an average across lengths and the average hides the shape.  Broken
    out: length 16 passes at 0.98%, length 17 at 0.83%, falling to ~0 by
    length 21.  The worst case sits exactly on `_OPAQUE_MIN_CHARS`, which
    is also exactly the shape of a Google app-specific password: 16
    lowercase letters, no separators.  Measured on 20k of those, 0.86%
    survived while this test allowed one odd pair at every length.

    That is why a run shorter than `_OPAQUE_STRICT_WORD_CHARS` must have
    *zero* odd pairs instead of at most one.  It takes the app-password
    leak to 0.16% and costs one real package name (`pythonjsonlogger`,
    whose `nj` seam is not an English pair).  0.16% is not 0: a credential
    drawn from a lowercase-letters-only alphabet and matched by no named
    rule above still passes at that rate.  That residue is the price of
    the allowance.  Narrow it by naming the shape in _RULES, never by
    widening this test.
    """
    if not (segment.isascii() and segment.isalpha() and segment.islower()):
        return False
    if len(segment) > _OPAQUE_MAX_WORD_CHARS:
        return False
    if all(character in "abcdef" for character in segment):
        return False
    odd = sum(1 for left, right in zip(segment, segment[1:])
              if left + right not in _ENGLISH_PAIRS)
    if len(segment) < _OPAQUE_STRICT_WORD_CHARS:
        return odd == 0
    # One odd pair is the seam of a compound (`pythonjsonlogger`).
    return odd <= 1


def _segment_is_safe(segment: str) -> bool:
    if len(segment) < _OPAQUE_MIN_CHARS:
        return True
    # Counters, epochs and byte totals carry no credential shape.
    if segment.isdigit():
        return True
    return _looks_like_identifier(segment) or _reads_as_english(segment)


def _is_recognizable_segment(segment: str) -> bool:
    """Affirmative recognition only -- no "short, therefore safe" clause.

    `_segment_is_safe` passes anything below `_OPAQUE_MIN_CHARS`, which is
    the right call when a whole run is being judged: a run that short is
    not a credential.  It is the wrong call for the pieces of a run that
    has *already* been found to contain one.  A base64 blob broken up by
    `/` has short pieces too, and keeping them publishes a contiguous
    slice of the key -- measured at up to 86 characters of a 64-byte
    secret before this function existed.
    """
    if not segment:
        return True
    if segment.isdigit():
        return True
    return _looks_like_identifier(segment) or _reads_as_english(segment)


def _opaque_replacement(match: re.Match[str]) -> str:
    """Mask a run that is not demonstrably safe — as narrowly as the run's
    own shape allows.

    A run that contains `+` or `=` is base64 alphabet all the way through:
    its separators are payload, not structure, so surviving short pieces
    would be a prefix of a credential and the whole run goes.  A run
    separated only by `/` is path-shaped — a URL authority, a filesystem
    path, a registry reference — and there only the offending segment goes.
    Condemning the whole run there used to take the rest of the path with
    it: `/var/lib/<long>/blobs/sha256` collapsed to a single placeholder,
    and `https://<long>.example.com` lost its `//` as well as its host,
    leaving output that no longer even reads as a URL.
    """
    run = match.group(0)
    if all(_segment_is_safe(part) for part in _OPAQUE_SPLIT_RE.split(run)):
        return run
    if "+" in run or "=" in run:
        return "[REDACTED-OPAQUE]"
    return "".join(
        piece if piece.startswith("/") or _is_recognizable_segment(piece)
        else "[REDACTED-OPAQUE]"
        for piece in _OPAQUE_PATH_SPLIT_RE.split(run)
    )


def _already_redacted(value: str) -> bool:
    """A placeholder this module just wrote is not a credential.

    Without this, a later rule re-matches `[REDACTED-HEADER]`, replaces the
    part before its closing bracket, and leaves a stray `]` behind.
    """
    return value.lstrip("\"'").startswith("[REDACTED")


def _proxy_uri_replacement(match: re.Match[str]) -> str:
    return f"{match.group(1)}://[REDACTED-PROXY-URI]"


def _url_replacement(match: re.Match[str]) -> str:
    scheme, rest = match.group(1), match.group(2)
    trailer = ""
    query = rest.find("?")
    fragment = rest.find("#")
    cuts = [index for index in (query, fragment) if index >= 0]
    if cuts:
        cut = min(cuts)
        trailer = ("?[REDACTED-QUERY]" if cut == query
                   else "#[REDACTED-FRAGMENT]")
        rest = rest[:cut]
    slash = rest.find("/")
    authority = rest if slash < 0 else rest[:slash]
    path = "" if slash < 0 else rest[slash:]
    if "@" in authority:
        authority = "[REDACTED-USERINFO]@" + authority.rsplit("@", 1)[1]
    return f"{scheme}://{authority}{path}{trailer}"


def _header_replacement(match: re.Match[str]) -> str:
    if _already_redacted(match.group(3)):
        return match.group(0)
    return f"{match.group(1)}{match.group(2)}[REDACTED-HEADER]"


def _bearer_replacement(match: re.Match[str]) -> str:
    return f"{match.group(1)} [REDACTED-BEARER]"


def _assignment_replacement(match: re.Match[str]) -> str:
    # A bare count ("n_tokens": 9571234) is the diagnostic, not the secret.
    if match.group(2).isdigit() or _already_redacted(match.group(2)):
        return match.group(0)
    return f"{match.group(1)}[REDACTED]"


def _github_replacement(_match: re.Match[str]) -> str:
    return "[REDACTED-GITHUB-TOKEN]"


def _google_api_key_replacement(_match: re.Match[str]) -> str:
    return "[REDACTED-GOOGLE-API-KEY]"


# Ordered: structural rules that consume whole values run before the
# shape-based ones, and the unrecognized-run catch-all runs last so it never
# re-examines a placeholder.
_Replacement = Callable[[re.Match[str]], str]
_RULES: tuple[tuple[str, re.Pattern[str], _Replacement], ...] = (
    ("PROXY-URI", _PROXY_URI_RE, _proxy_uri_replacement),
    ("URL", _URL_RE, _url_replacement),
    ("HEADER", _HEADER_RE, _header_replacement),
    ("BEARER", _BEARER_RE, _bearer_replacement),
    ("CREDENTIAL-FIELD", _STRONG_ASSIGNMENT_RE, _assignment_replacement),
    ("CREDENTIAL-FIELD", _WEAK_ASSIGNMENT_RE, _assignment_replacement),
    ("GITHUB-TOKEN", _GITHUB_TOKEN_RE, _github_replacement),
    ("GOOGLE-API-KEY", _GOOGLE_API_KEY_RE, _google_api_key_replacement),
)


def redact_diagnostic_text(text: str) -> tuple[str, list[str]]:
    """Redact free-form agent output for storage in ``client_meta``.

    Returns ``(redacted_text, labels)``.  ``labels`` names the rule families
    that fired, so an operator reading a capture knows what was removed.
    Over-redaction is intentional and is never reported as an error.
    """
    labels: set[str] = set()
    for label, pattern, replacement in _RULES:
        fired = False

        def apply(match: re.Match[str], _replacement=replacement) -> str:
            nonlocal fired
            produced = _replacement(match)
            if produced != match.group(0):
                fired = True
            return produced

        text = pattern.sub(apply, text)
        if fired:
            labels.add(label)

    # scrub.py's own credential shapes (sk-/sk-ant-/ghp_/JWT/Fernet/...)
    # plus mail addresses and home-directory paths.
    scrubbed = scrub_text(text)
    if scrubbed != text:
        labels.add("KNOWN-SHAPE")
    text = scrubbed

    catch_all = _OPAQUE_RUN_RE.sub(_opaque_replacement, text)
    if catch_all != text:
        labels.add("OPAQUE")
    return catch_all, sorted(labels)


# --------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------

def _reason(code: str) -> str:
    """Bounded, punctuation-free reason codes only — never file content."""
    return re.sub(r"[^a-z0-9_]", "", str(code).lower())[:48] or "unknown"


def agent_exited_non_zero(result_path: Path | None) -> bool:
    """True only for a recorded non-zero agent command exit.

    A completed run has nothing to explain, and its stderr would add weight
    to every submission for nothing.  result.json is read plainly here
    because runner.summarize_result and runner.diagnose_exception already
    read that same file the same way; this adds no new exposure.
    """
    if result_path is None:
        return False
    try:
        if not result_path.is_file():
            return False
        if result_path.stat().st_size > _RESULT_READ_LIMIT_BYTES:
            return False
        data = json.loads(result_path.read_text(encoding="utf-8",
                                                errors="replace"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    info = data.get("exception_info")
    if not isinstance(info, dict):
        return False
    return info.get("exception_type") == "NonZeroAgentExitCodeError"


def _read_stderr_tail(
    trial_dir: Path, *, tail_bytes: int,
) -> tuple[str | None, bytes, int]:
    """Return (source_name, tail_bytes_read, full_size).

    ``source_name`` is None when no adapter stderr artifact exists at all.
    Raises only what the artifact boundary raises; the caller converts that
    into a recorded reason.
    """
    with TrialFiles(trial_dir) as files:
        seen_empty: str | None = None
        for name in STDERR_CANDIDATES:
            relative = Path(_AGENT_LOG_DIR) / name
            if not files.exists(relative):
                continue
            data = files.read(relative, max_bytes=READ_LIMIT_BYTES)
            if not data:
                seen_empty = seen_empty or name
                continue
            return name, data[-tail_bytes:], len(data)
        return seen_empty, b"", 0


def collect_agent_stderr(
    trial_dir: Path, *, tail_bytes: int = TAIL_BYTES,
) -> dict:
    """Capture and redact the agent stderr tail.  Never raises.

    Always returns a status; a capture that could not happen is reported as
    ``unavailable`` with a bounded reason rather than silently omitted, so a
    missing capture is distinguishable from a capture that was never tried.
    """
    try:
        source, tail, size = _read_stderr_tail(
            Path(trial_dir), tail_bytes=tail_bytes,
        )
    except FileNotFoundError:
        return {
            "agent_stderr_status": STATUS_UNAVAILABLE,
            "agent_stderr_unavailable_reason": "trial_files_missing",
        }
    except UnsafeArtifact as exc:
        return {
            "agent_stderr_status": STATUS_UNAVAILABLE,
            "agent_stderr_unavailable_reason": f"boundary_{_reason(exc)}",
        }
    except OSError as exc:
        errno = getattr(exc, "errno", None)
        return {
            "agent_stderr_status": STATUS_UNAVAILABLE,
            "agent_stderr_unavailable_reason": (
                _reason(f"errno_{errno}") if errno else "io_error"
            ),
        }
    except Exception:  # noqa: BLE001 - diagnostics must never fail a submission
        return {
            "agent_stderr_status": STATUS_UNAVAILABLE,
            "agent_stderr_unavailable_reason": "collector_error",
        }

    if source is None:
        return {
            "agent_stderr_status": STATUS_UNAVAILABLE,
            "agent_stderr_unavailable_reason": "no_stderr_artifact",
        }
    if not tail:
        return {
            "agent_stderr_status": STATUS_EMPTY,
            "agent_stderr_source": source,
            "agent_stderr_bytes": size,
        }

    truncated = len(tail) < size
    text = tail.decode("utf-8", errors="replace")
    if truncated:
        # The byte cut lands mid-line; a half line is not evidence.
        newline = text.find("\n")
        if newline != -1:
            text = text[newline + 1:]
    try:
        redacted, labels = redact_diagnostic_text(text)
    except Exception:  # noqa: BLE001 - never upload unredacted text
        return {
            "agent_stderr_status": STATUS_UNAVAILABLE,
            "agent_stderr_source": source,
            "agent_stderr_bytes": size,
            "agent_stderr_unavailable_reason": "redaction_failed",
        }
    if len(redacted) > MAX_REDACTED_CHARS:
        redacted = redacted[-MAX_REDACTED_CHARS:]
        truncated = True
    return {
        "agent_stderr_status": STATUS_CAPTURED,
        "agent_stderr_source": source,
        "agent_stderr_bytes": size,
        "agent_stderr_truncated": truncated,
        "agent_stderr_redaction_labels": labels,
        "agent_stderr_tail": redacted,
    }


def collect_for_agent_exit(
    trial_dir: Path,
    result_path: Path | None,
    *,
    tail_bytes: int = TAIL_BYTES,
) -> dict:
    """client_meta fragment for a failed run; ``{}`` when the agent did not
    exit non-zero.  Must be called before the trial's container and volumes
    are torn down."""
    if not agent_exited_non_zero(result_path):
        return {}
    return collect_agent_stderr(trial_dir, tail_bytes=tail_bytes)


__all__ = [
    "MAX_REDACTED_CHARS",
    "READ_LIMIT_BYTES",
    "STDERR_CANDIDATES",
    "TAIL_BYTES",
    "agent_exited_non_zero",
    "collect_agent_stderr",
    "collect_for_agent_exit",
    "redact_diagnostic_text",
]
