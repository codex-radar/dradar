"""Say what the model platform answered when a run failed authentication.

``classify_exception_message`` decides *that* a failure is ``auth`` (and so
account-terminal); it does not say which answer the platform gave, and the
console used to print one generic line for every shape. A volunteer whose
API key the platform refused read the same advice as one whose request left
with no credential at all (#0226).

This module names the answer. It is a copy of dradar-server's
``auth_failure`` classification and English sentences -- the server derives
the same signal from the uploaded result.json for the site and the run-plan
progress -- so a volunteer reads the same sentence wherever they look. Keep
the two copies and their shared fixture file
(``tests/fixtures/auth_failure_messages.json``) in step.

Every sentence states what was observed. None says the volunteer's credential
is invalid, expired or misconfigured: we cannot see their side.
"""

from __future__ import annotations

import re

NO_CREDENTIAL = "no_credential"
API_KEY_REJECTED = "api_key_rejected"
LOGIN_REJECTED = "login_rejected"
ACCESS_FORBIDDEN = "access_forbidden"
PLATFORM_401 = "platform_401"
# No platform answer confirms it: only words in the agent's output.
AUTH_REPORTED = "auth_reported"

_REGION = (
    "user location is not supported",
    "country, region, or territory not supported",
    "unsupported_country_region_territory",
)
_NO_CREDENTIAL = (
    "missing bearer or basic authentication",
)
_API_KEY = (
    "incorrect api key provided",
    "invalid_api_key",
    "invalid api key",
    "the api key appears to be invalid",
    "api 密钥无效",
)
_API_KEY_MASKED = re.compile(r"your api key:\s*\S+\s+is invalid")
_LOGIN = (
    "access token could not be refreshed",
    "could not parse your authentication token",
    "authentication token has been invalidated",
    "invalidated oauth token",
    "invalid_grant",
    "provided authorization grant is invalid",
    "oauth refresh was rejected",
    "refresh_token_reused",
    "token expired",
    "token_expired",
)
_ACCOUNT = (
    "account suspended",
    "account disabled",
    "account deactivated",
)
_WEBSOCKET = (
    "responses_websocket",
    "wss://chatgpt.com/backend-api/codex/responses",
    "wss://api.openai.com/v1/responses",
)


def _has_http_status(low: str, status: int) -> bool:
    code = str(status)
    return bool(re.search(
        rf"(?:\bhttp(?:\s+status)?\s*|[\"']?(?:status|status_code|code)"
        rf"[\"']?\s*[:=]\s*){code}\b|"
        rf"\b{code}\s+(?:unauthorized|forbidden)",
        low,
    ))


def _agent_output(message: str) -> str:
    """Drop Pier's leading ``Command failed (exit N): <command>`` block: the
    command embeds the task instruction, which may well be about HTTP auth."""
    index = message.find("\nstdout:")
    return message[index:] if index >= 0 else message


def auth_failure_signal(message: object) -> str:
    """The platform's answer for a message already classified ``auth``."""
    if not isinstance(message, str) or not message:
        return AUTH_REPORTED
    low = _agent_output(message).lower()
    if any(marker in low for marker in _REGION):
        return AUTH_REPORTED
    if any(marker in low for marker in _NO_CREDENTIAL):
        return NO_CREDENTIAL
    if any(marker in low for marker in _API_KEY) or _API_KEY_MASKED.search(low):
        return API_KEY_REJECTED
    if any(marker in low for marker in _LOGIN):
        return LOGIN_REJECTED
    if any(marker in low for marker in _ACCOUNT):
        return ACCESS_FORBIDDEN
    if _has_http_status(low, 403) and not any(m in low for m in _WEBSOCKET):
        return ACCESS_FORBIDDEN
    if _has_http_status(low, 401):
        return PLATFORM_401
    return AUTH_REPORTED


_LEAD = {
    NO_CREDENTIAL: (
        "In this run the agent's requests to the model platform carried no "
        "credential, and the platform refused them (HTTP 401: Missing bearer "
        "or basic authentication)."
    ),
    API_KEY_REJECTED: (
        "The model platform refused the API key this run used (HTTP 401: "
        "Incorrect API key provided / invalid_api_key)."
    ),
    LOGIN_REJECTED: (
        "The model platform did not accept the sign-in token this run used "
        "(it answered that the token could not be refreshed or the grant was "
        "not accepted, and asked for a new sign-in)."
    ),
    ACCESS_FORBIDDEN: (
        "The model platform refused access for this run (HTTP 403, or an "
        "answer that the account is not available)."
    ),
    PLATFORM_401: (
        "The model platform answered this run's requests with HTTP 401 "
        "(not authenticated)."
    ),
    AUTH_REPORTED: (
        "The client classified this run as an authentication failure; no "
        "specific answer from the platform was recognised."
    ),
}

_TAIL = (
    "This run is therefore invalid and earns no points; if the model was "
    "called during the run, that usage counts against your own account "
    "quota. To check: run `dradar doctor` (it checks that the local sign-in "
    "file is present; it does not verify the credential with the platform). "
    "Questions: use the ✉️ Radar Mailbox on the site."
)


def auth_failure_sentence(signal: str) -> str:
    """One console sentence; identical to the server's English text."""
    return _LEAD.get(signal, _LEAD[AUTH_REPORTED]) + " " + _TAIL
