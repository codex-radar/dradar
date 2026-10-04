"""Extension for SecurityMind/pier fd5d8f1 only. No global Pier patching."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
from pathlib import Path

from pier.environments.base import BaseEnvironment, ExecResult
from pier.environments.docker.docker import DockerEnvironment

from .core import (BackendOperationError, Binding, BindingError, Command, Inspector, atomic_private_json,
                   classify_backend_failure,
                   compose_file_hashes, docker_command, full_image_id, normalize_platform,
                   require, state_record, tree_hash)
from .source_fence import verify_pinned_pier


class BoundPrebuiltDockerEnvironment(DockerEnvironment):
    def __init__(self, *, binding_path: str, inspection_command: Command = docker_command, **kwargs):
        # Fingerprint the installed source, not just a pip version or ensure_pier success.
        verify_pinned_pier()
        known = set(inspect.signature(DockerEnvironment.__init__).parameters)
        known |= set(inspect.signature(BaseEnvironment.__init__).parameters)
        known -= {"self", "args", "kwargs"}
        require(not set(kwargs) - known, "unknown environment constructor argument")
        self.binding_path = Path(binding_path)
        self.binding = Binding.load(self.binding_path)
        self.inspector = Inspector(self.binding, inspection_command)
        self._binding_phase = "unsubmitted"
        self._runtime_image_id: str | None = None
        self._generated_dockerfile_hash: str | None = None
        self._generated_context_hash: str | None = None
        self._build_completed = False
        self._compose_hashes = {}
        self._project_resources = None
        self._auxiliary_images = {}
        super().__init__(**kwargs)
        b = self.binding
        require(not self._uses_compose, "custom compose is not supported by immutable binding")
        require(not (self.environment_dir / ".env").exists(), "implicit compose dotenv input unsupported")
        for name in ("agent-build-context", "egress-proxy", "docker-compose-resources.json", "docker-compose-mounts.json", "docker-compose-egress-proxy.json"):
            require(not (self.trial_paths.trial_dir / name).exists(), "prior attempt-generated context/config exists")
        require(not self._is_windows_container, "Windows image binding is not supported")
        require(not self._keep_containers, "retained runtime containers are not supported by binding")
        require(self.task_env_config.docker_image == b.repository_digest, "effective task base is not pinned")
        require(str(self.environment_dir.resolve()) == b.environment_dir, "environment scope mismatch")
        require(str(self.trial_paths.trial_dir.resolve()) == b.trial_dir, "trial scope mismatch")
        require(self.session_id == b.session_id, "session scope mismatch")
        require(tree_hash(Path(b.selected_task_root)) == b.selected_task_hash, "selected task content changed")
        self._validate_selector_environment()
        self._validate_private_mounts()
        # Direct prebuilt also uses immutable image ID; installer FROM remains repo@digest.
        self._env_vars.prebuilt_image_name = b.base_image_id

    def _validate_selector_environment(self) -> None:
        reserved = set(self._env_vars.to_env_dict(include_os_env=False))
        reserved |= {"BUILDX_BUILDER", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH",
                     "COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "COMPOSE_PROFILES", "COMPOSE_ENV_FILES", "COMPOSE_BAKE",
                     "DOCKER_DEFAULT_PLATFORM", "PREBUILT_IMAGE_NAME", "DOCKER_BUILDKIT", "COMPOSE_DOCKER_CLI_BUILD", "BUILDX_CONFIG", "DOCKER_CONFIG"}
        user = set(self.task_env_config.env or {}) | set(self._persistent_env)
        require(not reserved & user, "task/persistent environment selector collision")
        require(not any(os.environ.get(k) for k in ("COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "COMPOSE_PROFILES", "COMPOSE_ENV_FILES", "COMPOSE_BAKE")), "ambient compose selector override")
        require(os.environ.get("DOCKER_BUILDKIT") != "0" and os.environ.get("COMPOSE_DOCKER_CLI_BUILD") != "0", "BuildKit bypass prohibited")
        platform = os.environ.get("DOCKER_DEFAULT_PLATFORM")
        require(platform in (None, self.binding.platform), "ambient Docker platform mismatch")

    def _validate_private_mounts(self) -> None:
        require(bool(self._mounts_json), "task-private log mounts required")
        for mount in self._mounts_json:
            require(mount.get("type") == "bind", "only task-private bind mounts supported")
            source = Path(mount.get("source", ""))
            require(source.is_absolute() and source.resolve() == source and source.is_relative_to(Path(self.binding.trial_dir)), "foreign mount source")
            for record in (self.binding_path, Path(self.binding.state_path), Path(self.binding.proof_path)):
                require(not record.is_relative_to(source), "private image binding record would be mounted")

    def _write_resources_compose_file(self) -> Path:
        # Preserve the stock resource helper bytes, but keep the consumed config
        # under the exact attempt instead of an unrelated /tmp directory.
        original = super()._write_resources_compose_file()
        path = self.trial_paths.trial_dir / "docker-compose-resources.json"
        require(not path.exists(), "prior resources config exists")
        path.write_bytes(original.read_bytes())
        self._cleanup_resources_compose_file()
        return path

    def _check_compose_inputs(self, *, add_current: bool = False) -> None:
        if self._compose_hashes:
            require(compose_file_hashes(list(self._compose_hashes)) == self._compose_hashes, "compose inputs changed")
        if add_current:
            self._compose_hashes.update(compose_file_hashes([str(p.resolve()) for p in self._docker_compose_paths]))

    def _persist_state(self, phase: str, *, exclusive: bool = False, **extra) -> None:
        record = state_record(self.binding, phase, runtime_image_id=self._runtime_image_id,
                              build_completed=self._build_completed,
                              generated_dockerfile_hash=self._generated_dockerfile_hash,
                              generated_context_hash=self._generated_context_hash,
                              compose_file_hashes=self._compose_hashes,
                              project_resources=self._project_resources, **extra)
        atomic_private_json(Path(self.binding.state_path), record, exclusive=exclusive)
        self._binding_phase = phase

    def _prepare_agent_build_context(self) -> None:
        super()._prepare_agent_build_context()
        if self.agent_install_spec is None:
            return
        require(self._agent_build_context_dir == Path(self.binding.trial_dir) / "agent-build-context", "installer context scope mismatch")
        path = self._agent_build_context_dir / "Dockerfile"
        data = path.read_bytes()
        lines = data.decode().splitlines()
        require(lines and lines[0] == f"FROM {self.binding.repository_digest}", "installer FROM is not pinned")
        require(sum(line.strip().upper().startswith("FROM ") for line in lines) == 1, "unexpected installer FROM override")
        self._generated_dockerfile_hash = hashlib.sha256(data).hexdigest()
        self._generated_context_hash = tree_hash(self._agent_build_context_dir)

    async def _validate_image_os(self, image_name: str) -> None:
        require(self._runtime_image_id is not None, "runtime image was not captured")
        await self.inspector.image(self._runtime_image_id)

    async def _compose(self, command: list[str], check: bool, timeout_sec: int | None) -> ExecResult:
        # One explicit method for test injection of compose command results.
        try:
            return await super()._run_docker_compose_command(command, check, timeout_sec)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise BackendOperationError(classify_backend_failure(str(error))) from None

    async def _run_docker_compose_command(self, command: list[str], check: bool = True,
                                         timeout_sec: int | None = None) -> ExecResult:
        require(command and not any(x == "--rmi" or x.startswith("--rmi=") for x in command), "image-removing compose teardown prohibited")
        self._validate_selector_environment()
        await self.inspector.same_daemon()
        action = command[0]
        if action == "build":
            require(self._binding_phase == "prepared" and self.agent_install_spec is not None, "unexpected build submission")
            require(self._generated_context_hash == tree_hash(self._agent_build_context_dir), "installer context changed before build")
            if self._egress_proxy_compose_path:
                proxy_config = json.loads(self._egress_proxy_compose_path.read_text())
                service = proxy_config["services"]["pier-egress-proxy"]
                if not service.get("build"):
                    selector = service.get("image")
                    full_image_id(selector)
                    require(selector == os.environ.get("DRADAR_EGRESS_PROXY_IMAGE"),
                            "proxy selector differs from prepared runtime")
                    raw = await self.inspector.json("image", "inspect", selector)
                    require(isinstance(raw, list) and len(raw) == 1, "ambiguous proxy image")
                    platform = "/".join(str(raw[0].get(key, "")) for key in ("Os", "Architecture"))
                    if raw[0].get("Variant") and raw[0].get("Architecture") != "amd64":
                        platform += "/" + raw[0]["Variant"]
                    platform = normalize_platform(platform)
                    require(service.get("platform", platform) == platform, "proxy platform selection mismatch")
                    proxy = await self.inspector.image(selector, platform=platform)
                    service.update(image=proxy["Id"], platform=platform, pull_policy="never")
                    self._egress_proxy_compose_path.write_text(json.dumps(proxy_config, indent=2))
                    self._auxiliary_images["pier-egress-proxy"] = proxy["Id"]
            self._check_compose_inputs(add_current=True)
            self._persist_state("build_submitted_unknown", quarantined=True)
            # Any non-success or cancellation retains unknown occupancy and base lease.
            result = await self._compose(command, check, timeout_sec)
            require(result.return_code == 0, "compose build did not succeed")
            await self.inspector.same_daemon()
            image = await self.inspector.image(f"{self.binding.project}-main:latest")
            require(self._generated_context_hash == tree_hash(self._agent_build_context_dir), "installer context changed during build")
            self._check_compose_inputs()
            if self._egress_proxy_compose_path and "pier-egress-proxy" not in self._auxiliary_images:
                proxy = await self.inspector.image(f"{self.binding.project}-pier-egress-proxy:latest")
                self._auxiliary_images["pier-egress-proxy"] = proxy["Id"]
            self._runtime_image_id = image["Id"]
            self._build_completed = True
            self._use_prebuilt = True
            self._env_vars.prebuilt_image_name = self._runtime_image_id
            self._persist_state("runtime_captured", quarantined=False)
            return result
        if action == "up":
            require(self._binding_phase in ("prepared", "runtime_captured"), "unexpected runtime submission")
            require(self._runtime_image_id is not None and self._use_prebuilt is True and self._env_vars.prebuilt_image_name == self._runtime_image_id, "up image selector is not frozen")
            require(self._DOCKER_COMPOSE_BUILD_PATH not in self._docker_compose_paths, "implicit rebuild during up prohibited")
            await self.inspector.project_empty()
            self._check_compose_inputs(add_current=True)
            self._persist_state("start_submitted_unknown", quarantined=True)
            # The generated filtered-egress proxy has its own build stanza.
            # Main-selector freeze alone must not permit an implicit auxiliary solve.
            frozen_command = [*command, "--no-build", "--pull", "never"]
            result = await self._compose(frozen_command, check, timeout_sec)
            require(result.return_code == 0, "compose up did not succeed")
            await self._capture_proof()
            return result
        if action == "down" and self._binding_phase in ("prepared", "runtime_captured"):
            # Fresh project is required. Never remove a stale/foreign predecessor.
            await self.inspector.project_empty()
            return ExecResult(stdout="", stderr="", return_code=0)
        if action in ("down", "stop"):
            require(self._binding_phase == "cleanup_pending", "cleanup has no fresh ownership verification")
        result = await self._compose(command, check, timeout_sec)
        await self.inspector.same_daemon()
        return result

    async def _capture_proof(self) -> None:
        await self.inspector.same_daemon()
        result = await self._compose(["ps", "--all", "--quiet", "--no-trunc", "main"], True, 20)
        ids = (result.stdout or "").split()
        require(result.return_code == 0 and len(ids) == 1, "ambiguous main container")
        paths = [str(p.resolve()) for p in self._docker_compose_paths]
        await self.inspector.container(ids[0], self._runtime_image_id, paths)
        self._check_compose_inputs()
        images = {"main": self._runtime_image_id, **self._auxiliary_images}
        self._project_resources = await self.inspector.project_resources(images, paths)
        require(self._project_resources["services"]["main"]["container_id"] == ids[0], "main inventory mismatch")
        await self.inspector.same_daemon()
        proof = state_record(self.binding, "started_proved", runtime_image_id=self._runtime_image_id,
                             container_id=ids[0], platform=self.binding.platform, compose_paths=paths,
                             generated_dockerfile_hash=self._generated_dockerfile_hash,
                             generated_context_hash=self._generated_context_hash,
                             build_completed=self._build_completed, quarantined=False,
                             compose_file_hashes=self._compose_hashes,
                             project_resources=self._project_resources)
        atomic_private_json(Path(self.binding.proof_path), proof, exclusive=True)
        self._persist_state("started_proved", container_id=ids[0], quarantined=False)

    async def start(self, force_build: bool):
        require(not force_build or self.agent_install_spec is not None, "force-build would bypass prebuilt binding")
        require(self.agent_install_spec is not None or self.task_env_config.allow_internet or not self.network_allowlist.domains,
                "direct prebuilt filtered-egress proxy requires explicit auxiliary preparation")
        require(self._binding_phase == "unsubmitted", "attempt cannot be restarted")
        await self.inspector.same_daemon()
        await self.inspector.image(self.binding.repository_digest, base=True)
        await self.inspector.project_empty()
        require(not Path(self.binding.proof_path).exists(), "prior proof exists")
        self._runtime_image_id = self.binding.base_image_id if self.agent_install_spec is None else None
        self._persist_state("prepared", exclusive=True, quarantined=False)
        try:
            await super().start(force_build)
        except BaseException:
            # Never turn unknown backend occupancy into success/absence on client exit.
            phase = "build_quarantined" if self._binding_phase == "build_submitted_unknown" else "attempt_quarantined"
            self._persist_state(phase, quarantined=True)
            raise

    async def stop(self, delete: bool):
        # Stock stop catches failures and delete=True removes images. Neither is valid here.
        # Unknown build/start is left quarantined for authoritative backend recovery.
        require(self._binding_phase == "started_proved", "unknown attempt cleanup is quarantined")
        await self.inspector.same_daemon()
        from .core import read_private_json
        proof = read_private_json(Path(self.binding.proof_path))
        await self.inspector.container(proof["container_id"], self._runtime_image_id, proof["compose_paths"])
        self._check_compose_inputs()
        images = {service: value["image_id"] for service, value in self._project_resources["services"].items()}
        require(await self.inspector.project_resources(images, proof["compose_paths"]) == self._project_resources,
                "project resources changed before cleanup")
        self._persist_state("cleanup_pending", quarantined=True)
        try:
            await self.prepare_logs_for_host()
            self._check_compose_inputs()
            await self._run_docker_compose_command(["down"])
            self._persist_state("cleanup_submitted", quarantined=True)
            # rc=0 is not confirmed_absent. Owner observer must establish absence and
            # successful build inactivity before independently releasing the base lease.
        except BaseException:
            self._persist_state("cleanup_quarantined", quarantined=True)
            raise
        finally:
            self._cleanup_resources_compose_file()
