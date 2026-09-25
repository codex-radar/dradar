"""Exercise Pier's Dockerfile selector/build gate without Docker or model calls."""
import asyncio
from types import SimpleNamespace
import tomllib

import pytest

from dradar import runner
from dradar.pier_codex import CodexRegistered
from pier.environments.docker.docker import DockerEnvironment
from pier.models.task.config import TaskConfig


def test_real_codex_install_and_pier_dockerfile_build_failure_recovery(tmp_path, monkeypatch):
    task = tmp_path / 'task'
    environment = task / 'environment'
    environment.mkdir(parents=True)
    source = 'FROM fixture:base\nCOPY fixture.txt /app/fixture.txt\n'
    (environment / 'Dockerfile').write_text(source)
    (environment / 'fixture.txt').write_text('bound task content')
    (task / 'task.toml').write_text('[environment]\nos="linux"\nallow_internet=false\n')
    config = TaskConfig.model_validate(tomllib.loads((task / 'task.toml').read_text()))
    monkeypatch.setenv('DOCKER_DEFAULT_PLATFORM', 'linux/arm64')
    assert runner._codex_task_platforms(task) == ('linux-arm64',)

    # Production subclass inherits the real install spec, including the native
    # binary smoke check. No constructor/login or install command is executed.
    agent = object.__new__(CodexRegistered)
    agent._version = '0.157.0'
    install = agent.install_spec()
    assert any('codex --version' in step.run for step in install.steps)
    calls = []
    fail_build = True

    async def compose(command):
        calls.append(command)
        if command[0] == 'build' and fail_build:
            raise RuntimeError('fixture registry pull failed')
    async def validate_image(image):
        calls.append(['validate-image', image])
    async def execute(command):
        calls.append(['exec', command])

    env = SimpleNamespace(
        agent_install_spec=install, _uses_compose=False, _is_windows_container=False,
        trial_paths=SimpleNamespace(trial_dir=tmp_path / 'trial'),
        task_env_config=config.environment, environment_dir=environment,
        environment_name='isolated-fixture', _resolve_user=lambda _: 'root',
        _env_vars=SimpleNamespace(context_dir=None, main_image_name='controlled-task-build'),
        _prepare_egress_proxy_compose=lambda: None,
        _write_resources_compose_file=lambda: None, _mounts_json=[],
        _validate_daemon_mode=lambda: None, _image_build_locks={},
        _build_command=DockerEnvironment._build_command,
        _run_docker_compose_command=compose, _validate_image_os=validate_image,
        exec=execute, _env_paths=SimpleNamespace(agent_dir='/logs/agent', verifier_dir='/logs/verifier'),
    )
    env._prepare_agent_build_context = lambda: DockerEnvironment._prepare_agent_build_context(env)
    with pytest.raises(RuntimeError, match='registry pull failed'):
        asyncio.run(DockerEnvironment.start(env, force_build=False))
    assert calls == [['build']]
    built = env._agent_build_context_dir
    assert (built / 'Dockerfile').read_text().startswith(source)
    assert (built / 'fixture.txt').read_text() == 'bound task content'
    assert '0.157.0' in (built / 'Dockerfile').read_text()
    assert (environment / 'Dockerfile').read_text() == source

    fail_build = False
    calls.clear()
    asyncio.run(DockerEnvironment.start(env, force_build=False))
    assert calls[:4] == [
        ['build'], ['validate-image', 'controlled-task-build'],
        ['down', '--remove-orphans'], ['up', '--detach', '--wait'],
    ]
