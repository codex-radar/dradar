"""Exercise collection-time isolation in a fresh pytest process."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.parametrize("inherited_home", [False, True])
@pytest.mark.parametrize("collection_error", [False, True])
def test_pytest_isolates_import_time_home_and_restores_environment(
    tmp_path, inherited_home, collection_error,
):
    project = tmp_path / "project"
    tests = project / "tests"
    tests.mkdir(parents=True)
    shutil.copyfile(Path(__file__).with_name("conftest.py"), tests / "conftest.py")
    caller_home = tmp_path / "caller-home"
    caller_home.mkdir()
    sentinel = caller_home / "config.json"
    sentinel.write_text('{"sentinel": true}', encoding="utf-8")
    report = project / "collected-home.txt"
    (tests / "test_probe.py").write_text(textwrap.dedent('''
        import os
        from pathlib import Path

        from dradar import local_config, run_plans

        # These assertions run during collection, before autouse fixtures.
        home = local_config.HOME
        assert home != Path(os.environ["PROBE_CALLER_HOME"])
        assert home != Path.home() / ".dradar"
        assert home == Path(os.environ["DRADAR_HOME"])
        assert local_config.CONFIG_PATH == home / "config.json"
        assert run_plans._root() == home / "run-plans"
        assert run_plans.stable_device.__defaults__ == (home,)
        assert local_config._load_config() == {}
        Path(os.environ["PROBE_REPORT"]).write_text(str(home), encoding="utf-8")

        if os.environ["PROBE_COLLECTION_ERROR"] == "1":
            raise RuntimeError("deliberate collection failure")

        def test_per_test_environment(tmp_path):
            assert Path(os.environ["DRADAR_HOME"]) == tmp_path / "dradar-home"
    '''), encoding="utf-8")
    env = os.environ.copy()
    env.pop("DRADAR_HOME", None)
    if inherited_home:
        env["DRADAR_HOME"] = str(caller_home)
    env.update({
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PROBE_CALLER_HOME": str(caller_home),
        "PROBE_REPORT": str(report),
        "PROBE_COLLECTION_ERROR": "1" if collection_error else "0",
    })
    script = textwrap.dedent('''
        import os
        from pathlib import Path
        import pytest

        original = os.environ.get("DRADAR_HOME")
        result = pytest.main(["-q", "tests", "--tb=short"])
        expected = 2 if os.environ["PROBE_COLLECTION_ERROR"] == "1" else 0
        assert result == expected, (result, expected)
        assert os.environ.get("DRADAR_HOME") == original
        report = Path(os.environ["PROBE_REPORT"])
        assert report.is_file(), "collection did not reach the isolated home"
        assert not Path(report.read_text(encoding="utf-8")).exists()
    ''')
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=project, env=env,
        text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert sentinel.read_text(encoding="utf-8") == '{"sentinel": true}'
    assert list(caller_home.iterdir()) == [sentinel]
