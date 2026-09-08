"""Host-side contract for authentication delivered to Pier task containers.

This module handles *model-provider* credentials, never the DRadar server
identity. Sources belong to the caller's machine; no ds0 service is required.
Vendor adapters keep their native auth format, refresh and persistence rules.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import ContextManager, Literal

Delivery = Literal['file-copy', 'directory-copy', 'shared-directory', 'process-token']
Refresh = Literal['native-cli', 'static', 'session-token']
Persistence = Literal['shared-store', 'validated-merge', 'container-only', 'unchanged']
SourceFactory = Callable[['AuthRequest', Mapping[str, Callable]], ContextManager[Path]]


class ContainerAuthError(ValueError):
    """An authentication boundary failed before starting the model runtime."""


@dataclass(frozen=True)
class AuthRequest:
    harness: str
    provider: str | None
    work_dir: Path = field(repr=False)


@dataclass(frozen=True)
class AuthCapabilities:
    delivery: Delivery
    refresh: Refresh
    persistence: Persistence
    # True only when the current transport exposes the provider's shared store.
    # It does not certify hot reload, server rotation semantics, or OS locks.
    shares_native_store: bool = False
    compatibility_exception: str | None = None


@dataclass(frozen=True)
class AuthBinding:
    harness: str
    provider: str
    source: Path = field(repr=False)
    source_kind: Literal['file', 'directory']
    capabilities: AuthCapabilities
    argument: str
    argument_channel: Literal['agent-kwarg', 'agent-env'] = 'agent-kwarg'

    def validate(self) -> None:
        if self.source_kind not in ('file', 'directory') or self.argument_channel not in ('agent-kwarg', 'agent-env'):
            raise ContainerAuthError('invalid credential delivery contract')
        capabilities = self.capabilities
        if (capabilities.delivery not in ('file-copy', 'directory-copy', 'shared-directory', 'process-token')
                or capabilities.refresh not in ('native-cli', 'static', 'session-token')
                or capabilities.persistence not in ('shared-store', 'validated-merge', 'container-only', 'unchanged')
                or capabilities.shares_native_store != (capabilities.delivery == 'shared-directory')):
            raise ContainerAuthError('inconsistent authentication capabilities')
        if capabilities.delivery in ('shared-directory', 'process-token') and not capabilities.compatibility_exception:
            raise ContainerAuthError('non-file delivery requires an explicit compatibility exception')
        exists = self.source.is_dir() if self.source_kind == 'directory' else self.source.is_file()
        if not exists:
            raise ContainerAuthError(f'{self.harness} credential {self.source_kind} is unavailable; prepare this provider before running')
        # Paths only. Secret contents never appear in argv, repr, or summaries.
        if any(char in str(self.source) for char in ('\x00', '\r', '\n')):
            raise ContainerAuthError('credential path contains a control character')
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', self.argument):
            raise ContainerAuthError('invalid credential argument contract')

    def pier_args(self) -> list[str]:
        self.validate()
        flag = '--ae' if self.argument_channel == 'agent-env' else '--ak'
        args = [flag, f'{self.argument}={self.source}']
        if self.capabilities.shares_native_store:
            args += ['--ak', 'shared_oauth=true']
        return args

    def environment_args(self, import_path: str, mounts: Callable[[str, Path], str]) -> list[str]:
        if not self.capabilities.shares_native_store:
            return []
        self.validate()
        return ['--environment-import-path', import_path,
                '--ek', 'shared_oauth_mounts_json=' + mounts(self.harness, self.source)]

    def summary(self) -> dict[str, str | bool]:
        """Non-secret capability facts; not a claim that authentication succeeded."""
        return {'harness': self.harness, 'provider': self.provider,
                'delivery': self.capabilities.delivery, 'refresh': self.capabilities.refresh,
                'persistence': self.capabilities.persistence,
                'shares_native_store': self.capabilities.shares_native_store,
                **({'compatibility_exception': self.capabilities.compatibility_exception}
                   if self.capabilities.compatibility_exception else {})}


@dataclass(frozen=True)
class AuthAdapter:
    harness: str
    provider: str
    source_factory: SourceFactory = field(repr=False)
    bind: Callable[[Path], AuthBinding] = field(repr=False)


class AuthRegistry:
    def __init__(self) -> None:
        self._adapters: dict[tuple[str, str], AuthAdapter] = {}
        self._defaults: dict[str, str] = {}

    def register(self, adapter: AuthAdapter, *, default: bool = False) -> None:
        key = (adapter.harness, adapter.provider)
        if key in self._adapters or (default and adapter.harness in self._defaults):
            raise ContainerAuthError('authentication adapter already registered')
        self._adapters[key] = adapter
        if default:
            self._defaults[adapter.harness] = adapter.provider

    def adapter(self, harness: str, provider: str | None = None) -> AuthAdapter | None:
        resolved = provider or self._defaults.get(harness)
        adapter = self._adapters.get((harness, resolved))
        if adapter is None and harness in self._defaults:
            raise ContainerAuthError(f'no authentication adapter for {harness} provider')
        return adapter

    def bind_existing(self, harness: str, provider: str | None, source: Path | None) -> AuthBinding | None:
        adapter = self.adapter(harness, provider)
        if adapter is None:
            if source is not None:
                raise ContainerAuthError('credentials cannot be delivered to an unregistered harness')
            return None
        if source is None:
            raise ContainerAuthError(f'{harness} credential is unavailable; prepare this provider before running')
        binding = adapter.bind(source)
        if (binding.harness, binding.provider) != (adapter.harness, adapter.provider):
            raise ContainerAuthError('authentication binding does not match its adapter')
        binding.validate()
        return binding

    @contextmanager
    def session(self, request: AuthRequest, hooks: Mapping[str, Callable]) -> Iterator[AuthBinding | None]:
        adapter = self.adapter(request.harness, request.provider)
        if adapter is None:
            yield None
            return
        # Native source contexts own validation, update merge and cleanup. Their
        # __exit__ sees the original body exception, including cancellation.
        with adapter.source_factory(request, hooks) as source:
            yield self.bind_existing(request.harness, request.provider, source)


def _source(hook: str, *, direct: bool = False, temporary: bool = False) -> SourceFactory:
    @contextmanager
    def acquire(request: AuthRequest, hooks: Mapping[str, Callable]) -> Iterator[Path]:
        factory = hooks[hook]
        if direct:
            yield factory()
        elif temporary:
            path = factory(request.work_dir)
            try:
                yield path
            finally:
                # Only files explicitly created for this session may be removed.
                path.unlink(missing_ok=True)
        else:
            with factory(request.work_dir) as path:
                yield path
    return acquire


def default_registry() -> AuthRegistry:
    from .providers import (
        DEFAULT_CODEX_PROVIDER, DEEPSEEK_PROVIDER, CLAUDE_AGENT, CLAUDE_PROVIDER,
        GROK_AGENT, GROK_PROVIDER, KIMI_AGENT, KIMI_PROVIDER,
        ANTIGRAVITY_AGENT, ANTIGRAVITY_PROVIDER, ZCODE_AGENT, ZCODE_PROVIDER, DSH_AGENT,
    )
    from .codebuddy_provider import CODEBUDDY_AGENT, CODEBUDDY_PROVIDER
    registry = AuthRegistry()

    def add(harness, provider, hook, argument, capabilities, *, kind='file',
            direct=False, temporary=False, channel='agent-kwarg', default=True):
        def bind(path):
            return AuthBinding(harness, provider, path, kind, capabilities, argument, channel)
        registry.register(AuthAdapter(harness, provider, _source(hook, direct=direct, temporary=temporary), bind), default=default)

    copied = AuthCapabilities('file-copy', 'native-cli', 'container-only')
    key = AuthCapabilities('file-copy', 'static', 'unchanged')
    shared = AuthCapabilities('shared-directory', 'native-cli', 'shared-store', True,
        'Preserve native shared storage until isolated-copy renewal coordination is validated')
    add('codex', DEFAULT_CODEX_PROVIDER, 'codex_auth_path', 'CODEX_AUTH_JSON_PATH', copied, direct=True, channel='agent-env')
    add('codex', DEEPSEEK_PROVIDER, 'create_deepseek_auth_json', 'CODEX_AUTH_JSON_PATH', key, temporary=True, channel='agent-env', default=False)

    def claude_bind(path):
        native = path.name == '.credentials.json'
        caps = copied if native else AuthCapabilities('process-token', 'session-token', 'unchanged',
            compatibility_exception='Official setup-token is consumed through the CLI process environment')
        return AuthBinding(CLAUDE_AGENT, CLAUDE_PROVIDER, path, 'file', caps,
                           'oauth_config_file' if native else 'oauth_token_file')
    registry.register(AuthAdapter(CLAUDE_AGENT, CLAUDE_PROVIDER, _source('claude_subscription_session'), claude_bind), default=True)
    add(GROK_AGENT, GROK_PROVIDER, 'grok_subscription_session', 'auth_json_file', shared)
    add(KIMI_AGENT, KIMI_PROVIDER, 'kimi_subscription_session', 'auth_json_file', shared)
    add(ANTIGRAVITY_AGENT, ANTIGRAVITY_PROVIDER, 'antigravity_subscription_session', 'auth_home_dir', shared, kind='directory')
    add(CODEBUDDY_AGENT, CODEBUDDY_PROVIDER, 'codebuddy_subscription_session', 'auth_dir', AuthCapabilities('directory-copy', 'native-cli', 'validated-merge'), kind='directory')
    add(ZCODE_AGENT, ZCODE_PROVIDER, 'create_zcode_api_key_file', 'api_key_file', key, temporary=True)
    add(DSH_AGENT, DEEPSEEK_PROVIDER, 'create_deepseek_api_key_file', 'api_key_file', key, temporary=True)
    return registry


AUTH_REGISTRY = default_registry()
