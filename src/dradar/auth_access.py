"""Access-only projections and generation-scoped consumption observations.

Material is never serialized by repr or diagnostics. These projections do not
verify signatures or prove that a provider will accept a credential. Providers
must declare their consumption contract before delivering a projection.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import base64
import hashlib
import hmac
import json
import math
import time

from .credential_files import credential_json


class AccessUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class AccessMaterial:
    provider: str
    token: str = field(repr=False)
    revision: str = field(repr=False)
    expires_at: float = field(repr=False)
    principal: str | None = field(default=None, repr=False)

    def usable(self, now: float | None = None, margin: float = 60) -> bool:
        return self.expires_at > (time.time() if now is None else now) + margin


def project_access(provider: str, content: bytes, *, local_key: bytes) -> AccessMaterial:
    """Read known token formats without persisting any new credentials.

    Codex JWT expiry is an untrusted scheduling hint, never identity validation.
    Explicit API keys are rejected instead of accidentally entering OAuth.
    """
    try:
        if not isinstance(local_key, bytes) or len(local_key) < 32:
            raise ValueError()
        data = credential_json(content)
        principal = None
        if provider == 'codex':
            if data.get('OPENAI_API_KEY') or data.get('auth_mode') == 'apikey':
                raise ValueError()
            token = data['tokens']['access_token']
            middle = token.split('.')[1]
            claims = json.loads(base64.urlsafe_b64decode(middle + '=' * (-len(middle) % 4)))
            expiry = claims['exp']
            account_id = data['tokens'].get('account_id')
            if isinstance(account_id, str) and account_id:
                principal = hmac.new(local_key, b'codex-account\0' + account_id.encode(), hashlib.sha256).hexdigest()[:32]
        elif provider == 'claude-code':
            auth = data['claudeAiOauth']
            if any(data.get(k) for k in ('apiKey', 'ANTHROPIC_API_KEY', 'anthropicApiKey')):
                raise ValueError()
            if 'user:inference' not in auth['scopes']:
                raise ValueError()
            token = auth['accessToken']
            expiry = auth['expiresAt'] / 1000
        else:
            raise AccessUnavailable('access_projection_unsupported')
        if not isinstance(token, str) or not token or len(token) > 64 * 1024 or any(c.isspace() for c in token):
            raise ValueError()
        if type(expiry) not in (float, int) or not math.isfinite(expiry) or expiry <= 0:
            raise ValueError()
        revision = hmac.new(local_key, provider.encode() + b'\0' + content, hashlib.sha256).hexdigest()[:32]
        return AccessMaterial(provider, token, revision, float(expiry), principal)
    except AccessUnavailable:
        raise
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        raise AccessUnavailable('access_projection_invalid') from None


@dataclass
class ConsumptionEvidence:
    """Facts apply to one generation, never inherited from a prior token."""
    material: AccessMaterial = field(repr=False)
    delivered: bool = False
    adopted: str = 'unknown'
    request: str = 'unknown'

    def delivered_generation(self, revision: str) -> None:
        if revision != self.material.revision:
            raise AccessUnavailable('generation_mismatch')
        self.delivered = True

    def adopted_generation(self, revision: str) -> None:
        if not self.delivered or revision != self.material.revision:
            raise AccessUnavailable('adoption_not_evidenced')
        self.adopted = 'confirmed'

    def request_result(self, revision: str, *, accepted: bool) -> None:
        if self.adopted != 'confirmed' or revision != self.material.revision or type(accepted) is not bool:
            raise AccessUnavailable('request_not_correlated')
        self.request = 'accepted' if accepted else 'rejected'

    def summary(self) -> dict[str, str | bool]:
        return {'provider': self.material.provider, 'delivered': self.delivered,
                'adopted': self.adopted, 'request': self.request}
