"""Resolve abbreviated task commits in the prepared source tree, before inference."""

import json
import os
from pathlib import Path
import re
import shlex
from urllib.parse import urlsplit

BASELINE_REQUEST_ENV = "DRADAR_TASK_BASELINE_REQUEST"
REMOTE_BASELINE = "/tmp/dradar-task-base-commit"


def repository_identity(value):
    if not isinstance(value, str):
        raise ValueError("task repository URL is missing")
    url = urlsplit(value)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.port not in (None, 443)):
        raise ValueError("task repository URL is not an approved HTTPS source")
    path = url.path.rstrip("/").removesuffix(".git")
    if not path or any(part in (".", "..", "") for part in path.split("/")[1:]):
        raise ValueError("task repository URL has an invalid path")
    return url.hostname.lower(), path


async def verify_task_baseline(environment):
    """Use only the built /app repository; never fetch or resolve a remote ref."""
    raw_path = os.environ.get(BASELINE_REQUEST_ENV)
    if not raw_path:
        return
    path = Path(raw_path)
    request = json.loads(path.read_text())
    prefix = request.get("base_commit")
    if not isinstance(prefix, str) or re.fullmatch(r"[0-9a-f]{4,39}", prefix) is None:
        raise ValueError("task baseline is not a hexadecimal commit abbreviation")
    expected_repository = repository_identity(request.get("repository_url"))

    async def git(arguments):
        result = await environment.exec(
            command="git --no-replace-objects -c safe.directory=/app -C /app " + arguments,
            env={"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"},
            timeout_sec=30,
        )
        if result.return_code != 0:
            raise ValueError("task baseline cannot be verified in the prepared source repository")
        return (result.stdout or "").strip()

    if await git("rev-parse --show-toplevel") != "/app":
        raise ValueError("task baseline source is not the prepared /app repository")
    if repository_identity(await git("remote get-url origin")) != expected_repository:
        raise ValueError("prepared source repository differs from the task declaration")
    matches = (await git("rev-parse --disambiguate=" + prefix)).splitlines()
    if len(matches) != 1 or re.fullmatch(r"[0-9a-f]{40}", matches[0]) is None:
        raise ValueError("task baseline abbreviation is missing or ambiguous")
    commit = matches[0]
    if not commit.startswith(prefix) or await git("cat-file -t " + commit) != "commit":
        raise ValueError("task baseline does not identify a commit object")
    # Persist the verified full object ID before any provider can run. The
    # collector consumes this full ID, never the abbreviation or mutable HEAD.
    result = await environment.exec(
        command="umask 022; printf '%s\\n' " + shlex.quote(commit)
        + " > " + REMOTE_BASELINE,
        timeout_sec=30,
    )
    if result.return_code != 0:
        raise ValueError("cannot persist the verified task baseline")
    evidence = {"base_commit": prefix, "resolved_commit": commit,
                "repository_url": request["repository_url"], "verified_before_model": True}
    evidence_path = path.with_suffix(".resolved.json")
    fd = os.open(evidence_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(evidence, stream)
