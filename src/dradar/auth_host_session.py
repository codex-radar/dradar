"""Compose authority, refresh, access projection and private delivery.

Vendor/runtime capability policy is supplied by a version-pinned adapter.
This class is not automatically installed in the legacy runner paths.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import json
import hmac
import os
import tempfile
from typing import Callable
import uuid

from .auth_access import AccessMaterial, ConsumptionEvidence, project_access
from .auth_authority import Authority
from .auth_refresh import AccessState, HostRefreshGate, RefreshUnavailable
from .pier_credential_delivery import inject_private_files


@dataclass
class HostAccessSession:
    authority: Authority = field(repr=False)
    local_key: bytes = field(repr=False)
    gate_root: Path = field(repr=False)
    renew: Callable[[], None] = field(repr=False)
    check_renewal_contract: Callable[[], None] = field(repr=False)
    observe: Callable[[dict], None] | None = field(default=None, repr=False)

    check_session_contract: Callable[[], None] | None = field(default=None, repr=False)
    renew_rejected: Callable[[], None] | None = field(default=None, repr=False)
    _principal: str = field(init=False, repr=False)

    def __post_init__(self):
        initial = project_access(self.authority.provider, self.authority.read(), local_key=self.local_key)
        if initial.principal is None:
            raise RefreshUnavailable('authority_identity_unverified')
        self._principal = initial.principal

    def _material(self) -> AccessMaterial:
        material = project_access(self.authority.provider, self.authority.read(), local_key=self.local_key)
        if material.principal != self._principal:
            raise RefreshUnavailable('authority_identity_changed')
        return material

    def _state(self) -> AccessState:
        material = self._material()
        return AccessState(material.revision, material.usable())

    def _observe(self, stage: str, status: str) -> None:
        if self.observe is not None:
            try:
                self.observe({'provider': self.authority.provider, 'auth_stage': stage,
                              'auth_status': status, 'auth_delivery': 'host-at'})
            except Exception:
                pass

    def prepare(self, *, rejected_revision: str | None = None) -> AccessMaterial:
        if self.check_session_contract is not None:
            self.check_session_contract()
        gate = HostRefreshGate(self.gate_root, self.authority.store_id)
        def state():
            current = self._state()
            return AccessState(current.revision, current.usable and current.revision != rejected_revision)
        if rejected_revision is not None and self.renew_rejected is None:
            raise RefreshUnavailable('rejected_renewal_unsupported')
        result = gate.ensure(state, self.renew if rejected_revision is None else self.renew_rejected,
                             before_renew=self.check_renewal_contract)
        material = self._material()
        # An external replacement between the locked read and projection must
        # not be attributed to the gate's successful generation.
        if material.revision != result.state.revision or not material.usable():
            raise RefreshUnavailable('authority_changed')
        self._observe('refresh', 'confirmed' if result.outcome == 'refreshed' else 'unknown')
        return material

    async def deliver(self, material: AccessMaterial, agent, environment) -> tuple[str, ConsumptionEvidence]:
        """Upload a new AT-only generation; no host bind or token argv.

        Returned location belongs to a DRadar-aware consumer, NOT stock codex
        exec. A consumer must explicitly adopt it; file delivery proves only
        transport. Every generation has a fresh private destination, so a
        failed new delivery cannot delete the prior generation.
        """
        if self.check_session_contract is not None:
            self.check_session_contract()
        if (material != self._material() or material.provider != self.authority.provider
                or material.principal != self._principal or not material.usable()):
            raise RefreshUnavailable('access_unavailable')
        destination = '/tmp/dradar-access-' + uuid.uuid4().hex + '/access.json'
        with tempfile.TemporaryDirectory(prefix='dradar-at-') as raw:
            source = Path(raw).resolve() / 'access.json'
            fd = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as target:
                json.dump({'access_token': material.token}, target)
            await inject_private_files(agent, environment, [(source, destination)])
        evidence = ConsumptionEvidence(material)
        evidence.delivered_generation(material.revision)
        self._observe('delivery', 'confirmed')
        return destination, evidence


def codex_host_session(authority: Authority, local_key: bytes, gate_root: Path,
                       rpc, check_renewal_contract: Callable[[], None], observe=None) -> HostAccessSession:
    """Bind the account-only RPC to the gate; no quota or model command."""
    from .auth_managed import ManagedAuthGuard, _check_runtime
    from .auth_codex_rpc import CodexAccountRpc
    if (not isinstance(check_renewal_contract, ManagedAuthGuard)
            or check_renewal_contract.authority != authority
            or not isinstance(rpc, CodexAccountRpc)):
        raise RefreshUnavailable('managed_custody_unverified')
    check_renewal_contract()
    if (gate_root != check_renewal_contract.store.root / 'gates'
            or not isinstance(local_key, bytes)
            or not hmac.compare_digest(local_key, check_renewal_contract.store._key())):
        raise RefreshUnavailable('managed_custody_unverified')
    if (rpc._executable != check_renewal_contract.executable
            or rpc._digest != _check_runtime(check_renewal_contract.executable)):
        raise RefreshUnavailable('managed_runtime_pin_mismatch')
    if authority.provider != 'codex':
        raise RefreshUnavailable('provider_mismatch')
    def renew(rejected=False):
        from .auth_transaction import refresh_codex_staged
        from .credential_files import read_private_credential
        intent = json.loads(read_private_credential(gate_root / authority.store_id / 'pending.json'))
        expected = intent.get('before')
        if (intent.get('schema') != 'dradar.refresh_intent.v2' or intent.get('state') != 'pending'
                or not isinstance(expected, str) or len(expected) != 32
                or any(char not in '0123456789abcdef' for char in expected)):
            raise RefreshUnavailable('recovery_required')
        refresh_codex_staged(authority, local_key, gate_root / authority.store_id / 'native', rpc,
                             expected_revision=expected, rejected=rejected)
    session = HostAccessSession(authority, local_key, gate_root, renew, check_renewal_contract, observe)
    session.renew_rejected = lambda: renew(rejected=True)
    return session
