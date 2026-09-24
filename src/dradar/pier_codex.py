"""Stock Codex adapter with the structured Pier worker lifecycle signal."""

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time

from pier.agents.installed.codex import Codex

try:
    from _dradar_credential_files import credential_json, read_private_credential
except ModuleNotFoundError as exc:
    if exc.name != "_dradar_credential_files":
        raise
    from dradar.credential_files import credential_json, read_private_credential

try:
    from _dradar_pier_credential_delivery import credential_upload_environment
except ModuleNotFoundError as exc:
    if exc.name != "_dradar_pier_credential_delivery":
        raise
    from dradar.pier_credential_delivery import credential_upload_environment

try:
    from _dradar_auth_codex_rpc import CodexAccountRpc, AccountRpcError
except ModuleNotFoundError as exc:
    if exc.name != "_dradar_auth_codex_rpc":
        raise
    from dradar.auth_codex_rpc import CodexAccountRpc, AccountRpcError

try:
    from _dradar_worker_events import register_worker, verify_task_baseline
except ModuleNotFoundError:
    from dradar.worker_events import register_worker, verify_task_baseline


try:
    from _dradar_artifact_boundary import private_post_run
except ModuleNotFoundError:
    from dradar.artifact_boundary import private_post_run


class NativeRenewalRequired(ValueError):
    """A native Codex renewal is needed before a read-only subscription check."""


class CodexRegistered(Codex):
    @staticmethod
    def _jwt_claims(value):
        # Claims are scheduling and identity hints only. The official Codex
        # account/read result below is the authority for account and plan.
        if not isinstance(value, str) or len(value) > 65536:
            raise ValueError("invalid token")
        parts = value.split(".")
        if len(parts) != 3:
            raise ValueError("invalid token")
        segment = parts[1]
        if len(segment) > 32768:
            raise ValueError("invalid token")
        claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
        if not isinstance(claims, dict):
            raise ValueError("invalid token")
        return claims

    def _verify_native_gpt6_subscription(self, snapshot, tokens):
        account_id = tokens.get("account_id")
        identity = self._jwt_claims(tokens.get("id_token"))
        access = self._jwt_claims(tokens.get("access_token"))
        email = identity.get("email")
        if (not isinstance(account_id, str) or not account_id
                or not isinstance(identity.get("sub"), str) or not identity["sub"]
                or not isinstance(email, str) or not email
                or type(access.get("exp")) is not int):
            raise ValueError("native identity verification required")
        if access["exp"] <= time.time() + 600:
            raise NativeRenewalRequired()
        for claims in (identity, access):
            account_claim = claims.get("chatgpt_account_id")
            auth_claim = claims.get("https://api.openai.com/auth")
            if isinstance(auth_claim, dict):
                account_claim = auth_claim.get("chatgpt_account_id", account_claim)
            if account_claim is not None and account_claim != account_id:
                raise ValueError("account identity mismatch")
        # The native read must use exactly the bytes later delivered to Pier.
        # A comfortably fresh access token prevents Codex's five-minute
        # proactive renewal from rotating a disposable copy's refresh token.
        with tempfile.TemporaryDirectory(prefix="dradar-gpt6-account-") as raw:
            root = Path(raw).resolve()
            auth = root / "auth.json"
            fd = os.open(auth, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as target:
                target.write(snapshot)
            executable = shutil.which("codex")
            if not executable:
                raise AccountRpcError("runtime_unavailable")
            executable = Path(executable).resolve(strict=True)
            with executable.open("rb") as binary:
                digest = hashlib.file_digest(binary, "sha256").hexdigest()
            status = CodexAccountRpc(executable, digest).subscription_status(
                root, expected_email=email,
            )
            if read_private_credential(auth) != snapshot:
                raise ValueError("native credential changed during verification")
        if status != "eligible":
            raise ValueError(status)

    def verify_gpt6_subscription_auth(self, source):
        if (self.model_name or "").split("/")[-1] not in ("gpt-6-sol", "gpt-6-luna"):
            return
        # These lanes are subscription-only. Reject an API credential before
        # Pier can deliver it to the task container; never echo auth contents.
        extra_env = getattr(self, "_extra_env", {}) or {}
        forbidden = (
            "OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN",
            "OPENAI_BASE_URL", "OPENAI_API_BASE",
        )
        if any((os.environ.get(key) or extra_env.get(key)) for key in forbidden):
            raise RuntimeError("GPT-6 trial requires ChatGPT subscription authentication")
        if not source:
            raise RuntimeError("GPT-6 trial requires ChatGPT subscription authentication")
        try:
            snapshot = read_private_credential(Path(source))
            data = credential_json(snapshot)
            tokens = data.get("tokens")
            if (
                data.get("auth_mode") not in (None, "chatgpt")
                or data.get("OPENAI_API_KEY")
                or not isinstance(tokens, dict)
                or not isinstance(tokens.get("access_token"), str)
                or not tokens["access_token"]
            ):
                raise ValueError("not subscription auth")
            if data.get("auth_mode") is None:
                self._verify_native_gpt6_subscription(snapshot, tokens)
        except NativeRenewalRequired:
            raise RuntimeError("GPT-6 ChatGPT login needs renewal with the official Codex CLI") from None
        except AccountRpcError:
            raise RuntimeError("GPT-6 ChatGPT subscription check is unavailable; retry later") from None
        except (OSError, ValueError, TypeError):
            raise RuntimeError("GPT-6 trial requires ChatGPT subscription authentication") from None
        return snapshot

    async def verify_gpt6_runtime(self, environment):
        if (self.model_name or "").split("/")[-1] not in ("gpt-6-sol", "gpt-6-luna"):
            return
        result = await self.exec_as_agent(
            environment, command=self.get_version_command(), timeout_sec=10,
        )
        versions = [line.strip() for line in (result.stdout or "").splitlines()
                    if line.strip().startswith("codex-cli ")]
        if result.return_code != 0 or versions != [f"codex-cli {self._version}"]:
            raise RuntimeError("GPT-6 Codex container version does not match the requested runtime")

    async def run(self, instruction, environment, context):
        gpt6 = (self.model_name or "").split("/")[-1] in ("gpt-6-sol", "gpt-6-luna")
        source = self._resolve_auth_json_path() if gpt6 else None
        snapshot = None
        if gpt6:
            snapshot = self.verify_gpt6_subscription_auth(source)
        await self.verify_gpt6_runtime(environment)
        await verify_task_baseline(environment)
        await register_worker(runtime="pier", context="agent", profile="codex")
        if not gpt6:
            source = self._resolve_auth_json_path()
            auth_environment = credential_upload_environment(environment, self, [source] if source else [])
            await super().run(instruction, auth_environment, context)
            return
        # Stock Pier resolves and uploads auth.json again inside run(). Pin both
        # reads to the same private snapshot that passed the subscription check.
        with tempfile.TemporaryDirectory(prefix="dradar-gpt6-auth-") as directory:
            # macOS can hand tempfile a /var path whose /var component is a
            # symlink; credential delivery deliberately refuses such paths.
            frozen = Path(directory).resolve() / "auth.json"
            fd = os.open(frozen, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
            with os.fdopen(fd, "wb") as handle:
                handle.write(snapshot)
            prior = self._extra_env.get("CODEX_AUTH_JSON_PATH")
            self._extra_env["CODEX_AUTH_JSON_PATH"] = str(frozen)
            try:
                auth_environment = credential_upload_environment(environment, self, [frozen])
                await super().run(instruction, auth_environment, context)
            finally:
                if prior is None:
                    self._extra_env.pop("CODEX_AUTH_JSON_PATH", None)
                else:
                    self._extra_env["CODEX_AUTH_JSON_PATH"] = prior

    @private_post_run
    def populate_context_post_run(self, context):
        return super().populate_context_post_run(context)
