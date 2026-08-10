# DeepSeek Official Endpoint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make PR #73 official-only while ensuring Codex configuration and Pier network policy use one fail-closed endpoint snapshot.

**Architecture:** `providers.py` owns one immutable official URL. `runner.py`
copies that value once while constructing a DeepSeek command, writes it into
Codex TOML, and passes it to the standalone Pier adapter. The adapter validates
the supplied value and never reads local DRadar configuration.

**Tech Stack:** Python 3.11+, pytest 8, argparse, public Pier adapter loaded
through Python import hooks in tests.

## Global Constraints

- The only supported URL is exactly `https://api.deepseek.com/`.
- Remove every runtime and CLI path for `opencode-go`.
- Local `config.json` must not select a DeepSeek endpoint.
- Do not add dependencies or speculative server assignment fields.
- Preserve the existing official credential path, capability, model catalog,
  runtime profile, and submission metadata.
- Do not create a git commit unless the user explicitly requests one.

## File Structure

- `src/dradar/providers.py`: define the immutable official DeepSeek URL and
  retain official credential/capability behavior.
- `src/dradar/runner.py`: snapshot the URL and feed the same value to Codex
  configuration and the Pier adapter.
- `src/dradar/pier_deepseek.py`: validate the supplied URL and construct the
  matching network allowlist without reading `config.json`.
- `src/dradar/cli.py`: expose only image-cache local configuration keys.
- `src/dradar/image_cache.py`: remove endpoint display and mutation.
- `tests/test_deepseek_pier_adapter.py`: test the standalone adapter with
  lightweight fake Pier modules.
- `tests/test_deepseek_provider.py`: test command-level endpoint consistency.
- `tests/test_image_cache.py`: test removal of the local endpoint surface.

---

### Task 1: Make the standalone Pier adapter fail closed

**Files:**
- Create: `tests/test_deepseek_pier_adapter.py`
- Modify: `src/dradar/pier_deepseek.py:11-95`

**Interfaces:**
- Consumes: `provider_base_url: str` supplied as a Pier agent constructor
  argument.
- Produces: `DeepSeekCodex(..., provider_base_url: str)` whose
  `network_allowlist()` uses that validated value.

- [ ] **Step 1: Add a test loader with fake Pier modules**

Create `tests/test_deepseek_pier_adapter.py`:

```python
import importlib.util
import sys
from pathlib import Path, PurePosixPath
from types import ModuleType

import pytest

import dradar
from dradar.providers import deepseek_catalog_path


OFFICIAL_BASE_URL = "https://api.deepseek.com/"


def _load_adapter(monkeypatch):
    calls = []

    def package(name):
        module = ModuleType(name)
        module.__path__ = []
        monkeypatch.setitem(sys.modules, name, module)
        return module

    package("pier")
    package("pier.agents")
    package("pier.agents.installed")
    package("pier.environments")
    package("pier.models")
    package("pier.models.agent")

    network = ModuleType("pier.agents.network")

    def allowlist_from_urls(urls, *, default_domains):
        calls.append((urls, default_domains))
        return "sentinel-allowlist"

    network.allowlist_from_urls = allowlist_from_urls
    monkeypatch.setitem(sys.modules, network.__name__, network)

    codex = ModuleType("pier.agents.installed.codex")

    class FakeCodex:
        _REMOTE_CODEX_HOME = PurePosixPath("/tmp/codex-home")

        def __init__(self, *args, **kwargs):
            pass

    codex.Codex = FakeCodex
    monkeypatch.setitem(sys.modules, codex.__name__, codex)

    environment = ModuleType("pier.environments.base")
    environment.BaseEnvironment = object
    monkeypatch.setitem(sys.modules, environment.__name__, environment)

    context = ModuleType("pier.models.agent.context")
    context.AgentContext = object
    monkeypatch.setitem(sys.modules, context.__name__, context)

    model_network = ModuleType("pier.models.agent.network")
    model_network.NetworkAllowlist = object
    monkeypatch.setitem(sys.modules, model_network.__name__, model_network)

    source = Path(dradar.__file__).with_name("pier_deepseek.py")
    spec = importlib.util.spec_from_file_location("_test_pier_deepseek", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, calls
```

- [ ] **Step 2: Add failing adapter behavior tests**

Append:

```python
def test_adapter_rejects_non_official_provider_url(monkeypatch):
    module, _calls = _load_adapter(monkeypatch)

    with pytest.raises(ValueError, match="unsupported DeepSeek provider URL"):
        module.DeepSeekCodex(
            model_catalog_json_file=str(deepseek_catalog_path()),
            provider_base_url="https://opencode.ai/zen/go/v1",
        )


def test_allowlist_uses_constructor_snapshot_not_local_config(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("DRADAR_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(
        '{"deepseek_endpoint":"opencode-go"}'
    )
    module, calls = _load_adapter(monkeypatch)
    adapter = module.DeepSeekCodex(
        model_catalog_json_file=str(deepseek_catalog_path()),
        provider_base_url=OFFICIAL_BASE_URL,
    )

    assert adapter.network_allowlist() == "sentinel-allowlist"
    assert calls == [([OFFICIAL_BASE_URL], [])]
```

- [ ] **Step 3: Run the tests and verify the expected failures**

Run:

```bash
.venv/bin/pytest \
  tests/test_deepseek_pier_adapter.py::test_adapter_rejects_non_official_provider_url \
  tests/test_deepseek_pier_adapter.py::test_allowlist_uses_constructor_snapshot_not_local_config \
  -q
```

Expected: the first test fails because the current constructor accepts the
third-party URL, and the second fails because the current allowlist reads
`config.json` and selects OpenCode Go.

- [ ] **Step 4: Implement constructor validation and snapshot use**

In `src/dradar/pier_deepseek.py`, remove the `json` and `os` imports and replace
the endpoint table and `_deepseek_base_url()` with:

```python
_OFFICIAL_DEEPSEEK_BASE_URL = "https://api.deepseek.com/"
```

Change `network_allowlist()` to:

```python
return allowlist_from_urls(
    [self._provider_base_url],
    default_domains=[],
)
```

Change the constructor to:

```python
def __init__(
    self,
    *args: Any,
    model_catalog_json_file: str,
    provider_base_url: str,
    **kwargs: Any,
):
    if provider_base_url != _OFFICIAL_DEEPSEEK_BASE_URL:
        raise ValueError(
            f"unsupported DeepSeek provider URL: {provider_base_url!r}"
        )
    self._provider_base_url = provider_base_url
    catalog = Path(model_catalog_json_file)
    try:
        digest = hashlib.sha256(catalog.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError(
            f"DeepSeek model catalog is unreadable: {catalog}"
        ) from exc
    if digest != _CATALOG_SHA256:
        raise ValueError(
            "DeepSeek model catalog integrity check failed; reinstall or "
            "upgrade dradar before running a paid task"
        )
    self._model_catalog_json_file = catalog
    super().__init__(*args, **kwargs)
```

- [ ] **Step 5: Run the adapter tests and verify they pass**

Run:

```bash
.venv/bin/pytest tests/test_deepseek_pier_adapter.py -q
```

Expected: `2 passed`.

### Task 2: Use one official URL snapshot in the runner

**Files:**
- Modify: `tests/test_deepseek_provider.py:16-240`
- Modify: `src/dradar/providers.py:17-66`
- Modify: `src/dradar/runner.py:32-90`
- Modify: `src/dradar/runner.py:316-318`
- Modify: `src/dradar/runner.py:441-576`

**Interfaces:**
- Consumes: `DEEPSEEK_BASE_URL: str` from `providers.py`.
- Produces: `deepseek_toml(base_url: str) -> str` and
  `_ensure_deepseek_config(home: Path, base_url: str) -> Path`.
- Produces: a Pier command containing
  `provider_base_url=https://api.deepseek.com/`.

- [ ] **Step 1: Add a failing command consistency test**

In `tests/test_deepseek_provider.py`, remove the local-config monkeypatch from
`_command()` and replace the three endpoint-selection tests with:

```python
def test_command_uses_one_official_endpoint_snapshot(
    tmp_path: Path,
    monkeypatch,
):
    command, _home = _command(tmp_path, monkeypatch)
    config_arg = next(
        item for item in command if item.startswith("config_toml_file=")
    )
    parsed = tomllib.loads(Path(config_arg.split("=", 1)[1]).read_text())
    config_url = parsed["model_providers"][DEEPSEEK_PROVIDER]["base_url"]

    assert config_url == "https://api.deepseek.com/"
    assert [
        item for item in command if item.startswith("provider_base_url=")
    ] == [f"provider_base_url={config_url}"]
```

- [ ] **Step 2: Run the new test and verify it fails**

Run:

```bash
.venv/bin/pytest \
  tests/test_deepseek_provider.py::test_command_uses_one_official_endpoint_snapshot \
  -q
```

Expected: FAIL because the command has no `provider_base_url=` agent argument.

- [ ] **Step 3: Replace selectable endpoints with one constant**

In `src/dradar/providers.py`, delete `DEEPSEEK_ENDPOINT_DEFAULT`,
`DEEPSEEK_ENDPOINTS`, and `deepseek_base_url()`. Add next to the other DeepSeek
constants:

```python
DEEPSEEK_BASE_URL = "https://api.deepseek.com/"
```

- [ ] **Step 4: Parameterize TOML materialization**

In `src/dradar/runner.py`, import `DEEPSEEK_BASE_URL` and remove the
`deepseek_base_url` import. Change:

```python
def deepseek_toml(base_url: str) -> str:
    return (
        'web_search = "disabled"\n'
        'model_provider = "deepseek"\n'
        'preferred_auth_method = "apikey"\n'
        'forced_login_method = "api"\n'
        f'model_catalog_json = "{DEEPSEEK_CATALOG_REMOTE_PATH}"\n'
        '[features]\n'
        'apps = false\n'
        'remote_plugin = false\n'
        '[model_providers.deepseek]\n'
        'name = "deepseek"\n'
        f'base_url = "{base_url}"\n'
        'wire_api = "responses"\n'
        'requires_openai_auth = true\n'
    )
```

Change the materializer to:

```python
def _ensure_deepseek_config(home: Path, base_url: str) -> Path:
    path = home / "codex-deepseek-v4-flash.toml"
    return _materialize_shared_file(path, deepseek_toml(base_url).encode())
```

- [ ] **Step 5: Snapshot and pass the URL during command construction**

In `build_pier_command()`, initialize and set the snapshot:

```python
deepseek_catalog = None
deepseek_provider_base_url = None
if provider == DEEPSEEK_PROVIDER:
    _validate_deepseek_assignment(assignment)
    deepseek_provider_base_url = DEEPSEEK_BASE_URL
    deepseek_catalog = _validated_deepseek_catalog()
```

In the DeepSeek command branch, require the snapshot and use it for both
outputs:

```python
if deepseek_provider_base_url is None:
    raise RunnerError("DeepSeek provider URL was not prepared")
config_path = _ensure_deepseek_config(home, deepseek_provider_base_url)
```

Add this agent argument beside `model_catalog_json_file`:

```python
"--ak", f"provider_base_url={deepseek_provider_base_url}",
```

- [ ] **Step 6: Update shared-input expectations**

In `test_deepseek_shared_inputs_are_reused_and_owner_only()`, use:

```python
home / "codex-deepseek-v4-flash.toml": runner.deepseek_toml(
    providers.DEEPSEEK_BASE_URL
).encode(),
```

and call:

```python
assert runner._ensure_deepseek_config(
    home, providers.DEEPSEEK_BASE_URL
) in expected
```

- [ ] **Step 7: Run provider and adapter tests**

Run:

```bash
.venv/bin/pytest \
  tests/test_deepseek_provider.py \
  tests/test_deepseek_pier_adapter.py \
  tests/test_provider_config.py \
  -q
```

Expected: all selected tests pass with no warnings.

### Task 3: Remove the local endpoint configuration surface

**Files:**
- Modify: `tests/test_image_cache.py:1-8`
- Modify: `tests/test_image_cache.py:314-326`
- Modify: `src/dradar/cli.py:214-224`
- Modify: `src/dradar/image_cache.py:667-722`

**Interfaces:**
- Consumes: existing `config show` and `config set` CLI commands.
- Produces: only `image-cache-mode` and `image-cache-limit-gb` as accepted
  local configuration keys.

- [ ] **Step 1: Add failing CLI and stale-config tests**

Add `cli` to the imports in `tests/test_image_cache.py`:

```python
from dradar import cli, image_cache, local_config, runloop
```

Append:

```python
def test_cli_rejects_deepseek_endpoint_setting(monkeypatch):
    monkeypatch.setattr(
        cli,
        "cmd_config_set",
        lambda _args: pytest.fail("removed endpoint setting must not dispatch"),
    )

    with pytest.raises(SystemExit) as exc:
        cli.main(["config", "set", "deepseek-endpoint", "opencode-go"])

    assert exc.value.code == 2


def test_config_show_ignores_stale_deepseek_endpoint(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(local_config, "HOME", tmp_path)
    monkeypatch.setattr(local_config, "CONFIG_PATH", tmp_path / "config.json")
    local_config._save_config({"deepseek_endpoint": "opencode-go"})

    assert image_cache.cmd_config_show(argparse.Namespace()) == 0

    assert "deepseek endpoint" not in capsys.readouterr().out
```

- [ ] **Step 2: Run the tests and verify both fail**

Run:

```bash
.venv/bin/pytest \
  tests/test_image_cache.py::test_cli_rejects_deepseek_endpoint_setting \
  tests/test_image_cache.py::test_config_show_ignores_stale_deepseek_endpoint \
  -q
```

Expected: the parser currently dispatches the endpoint setting, and config
output currently displays the stale endpoint.

- [ ] **Step 3: Remove the parser choice**

In `src/dradar/cli.py`, set:

```python
p_config_set.add_argument(
    "key",
    choices=("image-cache-mode", "image-cache-limit-gb"),
)
```

- [ ] **Step 4: Remove endpoint display and mutation**

In `cmd_config_show()`, remove the `providers` import and the two endpoint
display lines.

In `cmd_config_set()`, remove the `providers` import and the complete
`elif args.key == "deepseek-endpoint":` branch. The remaining structure is:

```python
if args.key == "image-cache-mode":
    value = args.value.strip().lower()
    if value not in {"balanced", "metered", "disk"}:
        raise SystemExit("image-cache-mode must be balanced, metered, or disk")
    cfg["image_cache_mode"] = value
    shown = value
else:
    value = args.value.strip().lower()
    if value == "auto":
        cfg.pop("image_cache_limit_gb", None)
        shown = "automatic"
    else:
        try:
            limit = float(value)
        except ValueError as exc:
            raise SystemExit(
                "image-cache-limit-gb must be a positive number or auto"
            ) from exc
        if limit <= 0:
            raise SystemExit(
                "image-cache-limit-gb must be greater than zero"
            )
        cfg["image_cache_limit_gb"] = limit
        shown = f"{limit:g} GiB"
```

- [ ] **Step 5: Run image-cache and CLI tests**

Run:

```bash
.venv/bin/pytest \
  tests/test_image_cache.py \
  tests/test_cli_interrupt.py \
  -q
```

Expected: all selected tests pass.

### Task 4: Verify scope, behavior, and regression safety

**Files:**
- Review: `src/dradar/providers.py`
- Review: `src/dradar/runner.py`
- Review: `src/dradar/pier_deepseek.py`
- Review: `src/dradar/image_cache.py`
- Review: `src/dradar/cli.py`
- Review: `tests/test_deepseek_provider.py`
- Review: `tests/test_deepseek_pier_adapter.py`
- Review: `tests/test_image_cache.py`

**Interfaces:**
- Consumes: the completed official-only implementation.
- Produces: evidence that no selectable endpoint remains and the full suite is
  green.

- [ ] **Step 1: Check for residual third-party endpoint paths**

Run:

```bash
rg -n \
  'opencode-go|deepseek_endpoint|deepseek-endpoint|deepseek_base_url|DEEPSEEK_ENDPOINT' \
  src
```

Expected: no matches. Security regression tests may still contain synthetic
third-party values to prove they are rejected or ignored.

- [ ] **Step 2: Run focused security and provenance regression tests**

Run:

```bash
.venv/bin/pytest \
  tests/test_deepseek_provider.py \
  tests/test_deepseek_pier_adapter.py \
  tests/test_provider_config.py \
  tests/test_go_menu.py \
  tests/test_image_cache.py \
  -q
```

Expected: all selected tests pass.

- [ ] **Step 3: Run the complete test suite**

Run:

```bash
.venv/bin/pytest -q
```

Expected: exit code 0 with no failures.

- [ ] **Step 4: Review the final diff**

Run:

```bash
git status --short
git diff --check
git diff --stat origin/main...HEAD
git diff
```

Expected: no whitespace errors; changes are limited to the approved design,
tests, and implementation. Do not commit or push without an explicit user
request.
