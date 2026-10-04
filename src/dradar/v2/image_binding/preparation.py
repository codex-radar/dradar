"""Private task overlay only; external resolution/coordinator remain owner hooks."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import shutil
import tomllib

from .core import BindingError, REPO_DIGEST, require, tree_hash

IMPORT_PATH = "dradar_v2_image_binding.pier_cache:AllocatedImageEnvironment"


def binding_cli_args(binding_path: Path | None) -> list[str]:
    """Append to pinned build_pier_command; None preserves historical argv exactly."""
    if binding_path is None:
        return []
    require(binding_path.is_absolute(), "binding CLI path must be absolute")
    return ["--environment-import-path", "dradar_v2_image_binding.pier_adapter:BoundPrebuiltDockerEnvironment",
            "--ek", f"binding_path={binding_path}", "--no-delete"]


def pin_private_task(source: Path, destination: Path, repository_digest: str) -> dict:
    """Copy after existing overlays; change only parsed environment.docker_image.

    Refuses a baseline overlay that removed docker_image. Does not label this dynamic
    Dockerfile PublicBuild-eligible, and does not equate task-tree and package hashes.
    """
    require(bool(REPO_DIGEST.fullmatch(repository_digest)), "invalid pinned repository digest")
    require(source.is_absolute() and source.resolve() == source and destination.is_absolute() and destination.resolve() == destination, "noncanonical task path")
    require(not destination.exists() and not destination.is_relative_to(source), "private destination already exists or overlaps")
    original_tree_hash = tree_hash(source)
    config = source / "task.toml"
    original = config.read_bytes()
    parsed = tomllib.loads(original.decode())
    environment = parsed.get("environment", {})
    require(isinstance(environment, dict) and isinstance(environment.get("docker_image"), str) and bool(environment["docker_image"]), "effective overlay removed prebuilt image")
    # Preserve all source bytes except one simple scalar. Unusual TOML remains a
    # fail-closed unsupported form rather than a lossy full-file serialization.
    lines = original.decode().splitlines(keepends=True)
    in_environment, count = False, 0
    for index, line in enumerate(lines):
        if re.match(r"^\s*\[", line):
            in_environment = bool(re.match(r"^\s*\[environment\]\s*(?:#.*)?$", line.rstrip("\r\n")))
        if in_environment and re.match(r"^\s*docker_image\s*=", line):
            require(re.match(r"^\s*docker_image\s*=\s*(?:\"[^\"\r\n]*\"|'[^'\r\n]*')\s*(?:#.*)?$", line.rstrip("\r\n")) is not None, "unsupported docker_image TOML scalar")
            indent = re.match(r"^\s*", line).group()
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            lines[index] = indent + "docker_image = " + json.dumps(repository_digest) + ending
            count += 1
    require(count == 1, "prebuilt image scalar not uniquely found")
    selected = "".join(lines).encode()
    expected = copy.deepcopy(parsed)
    expected["environment"]["docker_image"] = repository_digest
    require(tomllib.loads(selected.decode()) == expected, "private task overlay changed additional configuration")
    shutil.copytree(source, destination, symlinks=True)
    try:
        require(tree_hash(destination) == original_tree_hash, "copied task differs from verified effective source")
        (destination / "task.toml").write_bytes(selected)
        require(tree_hash(source) == original_tree_hash and config.read_bytes() == original, "source task mutated during overlay")
        return {"effective_task_hash": original_tree_hash, "selected_task_hash": tree_hash(destination),
                "original_image_ref": environment["docker_image"], "repository_digest": repository_digest}
    except BaseException:
        # Leave an untrusted attempt copy for owner-managed recovery, never publish it.
        raise
