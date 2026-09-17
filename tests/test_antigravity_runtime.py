"""No credentials/model calls: real Linux process trees and real git exports."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from dradar import antigravity_runtime as runtime


@unittest.skipUnless(sys.platform == "linux", "Linux container supervisor")
class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / 'app'
        self.repo.mkdir()
        def git(*args):
            return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True).strip()
        self.git = git
        git('init', '-q')
        git('config', 'user.email', 'fixture@example.invalid')
        git('config', 'user.name', 'fixture')
        (self.repo / 'file').write_text('base\n')
        git('add', '.')
        git('commit', '-qm', 'base')
        self.base = git('rev-parse', 'HEAD')
        (self.root / 'control').mkdir()
        self.config = dict(control=str(self.root / 'control'), artifacts=str(self.root / 'artifacts'),
            workspace=str(self.repo), base=self.base, run_id='a' * 32,
            stdout=str(self.root / 'stdout'), stderr=str(self.root / 'stderr'))

    def tearDown(self):
        self.temp.cleanup()

    def launch(self, code, prefix=''):
        config = dict(self.config, argv=[sys.executable, '-c', code])
        source = Path(runtime.__file__).read_text()
        if prefix:
            source = source.replace('if __name__ == "__main__":', prefix + '\nif __name__ == "__main__":')
        return subprocess.Popen([sys.executable, '-c', source, json.dumps(config)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def finish(self, proc):
        out, err = proc.communicate(timeout=14)
        first, last = [json.loads(line) for line in out.splitlines()]
        self.assertEqual(last['mac'], hmac.new(bytes.fromhex(first['key']), last['payload'].encode(), hashlib.sha256).hexdigest())
        return json.loads(last['payload'])

    def test_normal_nonzero_and_empty(self):
        for changed, code in [(True, 0), (True, 7), (False, 0)]:
            with self.subTest(changed=changed, code=code):
                self.git('reset', '--hard', self.base)
                proc = self.launch("from pathlib import Path; " +
                    ("Path('file').write_text('changed\\n'); Path('new').write_bytes(b'\\x00binary'); " if changed else "") +
                    f"raise SystemExit({code})")
                status = self.finish(proc)
                patch = self.root / 'artifacts/model.patch'
                self.assertTrue(status['writer_stopped'])
                self.assertTrue(status['exported'])
                self.assertEqual(status['patch_sha256'], hashlib.sha256(patch.read_bytes()).hexdigest())
                self.assertEqual(proc.returncode, code)
                self.git('reset', '--hard', self.base)
                self.git('clean', '-fdq')
                if changed:
                    self.git('apply', '--check', str(patch))
                else:
                    self.assertEqual(patch.read_bytes(), b'')

    def test_cancel_double_fork_and_setsid(self):
        ready = self.root / 'ready'
        code = f'''import os,time
from pathlib import Path
Path('calls').write_text('one')
if os.fork() == 0:
    os.setsid()
    if os.fork() == 0:
        Path({str(ready)!r}).write_text(str(os.getpid()))
        while True:
            Path('file').write_text('modified\\n')
            time.sleep(.01)
    os._exit(0)
while True: time.sleep(.1)
'''
        proc = self.launch(code)
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(ready.exists())
        (self.root / 'control/stop').touch()
        (self.root / 'control/stop').touch()
        start = time.monotonic()
        status = self.finish(proc)
        self.assertLess(time.monotonic() - start, 12)
        self.assertTrue(status['writer_stopped'])
        self.assertTrue(status['exported'])
        self.assertEqual((self.repo / 'calls').read_text(), 'one')
        self.assertFalse(Path('/proc/' + ready.read_text()).exists())
        self.git('reset', '--hard', self.base)
        self.git('clean', '-fdq')
        self.git('apply', '--check', str(self.root / 'artifacts/model.patch'))

    def test_stop_before_launch(self):
        (self.root / 'control/stop').touch()
        status = self.finish(self.launch("raise RuntimeError('must not run')"))
        self.assertFalse(status['exported'])
        self.assertFalse((self.root / 'stdout').exists())

    def test_export_failure_and_timeout_never_publish(self):
        for code in ["from pathlib import Path; Path('.git/index.lock').touch()"]:
            status = self.finish(self.launch(code))
            self.assertFalse(status['exported'])
            self.assertFalse((self.root / 'artifacts/model.patch').exists())
            (self.repo / '.git/index.lock').unlink(missing_ok=True)
        # Delay the actual Git diff child; bounded subprocess timeout must reap it.
        prefix = '''original_run = subprocess.run
def slow_run(args, **kwargs):
    if "diff" in args:
        args = [sys.executable, "-c", "import time; time.sleep(30)"]
    return original_run(args, **kwargs)
subprocess.run = slow_run'''
        start = time.monotonic()
        status = self.finish(self.launch("pass", prefix))
        self.assertFalse(status['exported'])
        self.assertEqual(status['error'], 'TimeoutExpired')
        self.assertLess(time.monotonic() - start, 10)
        self.assertFalse((self.root / 'artifacts/model.patch').exists())

    def test_untrusted_git_filters_and_hooks_are_disabled(self):
        marker = self.root / 'untrusted-ran'
        self.git('config', 'filter.evil.clean', f'touch {marker}; cat')
        self.git('config', 'filter.evil.process', f'touch {marker}')
        self.git('config', 'filter.evil.required', 'true')
        self.git('config', 'core.fsmonitor', f'touch {marker}')
        status = self.finish(self.launch("from pathlib import Path; Path('.gitattributes').write_text('* filter=evil\\n'); Path('file').write_text('new\\n')"))
        self.assertTrue(status['exported'])
        self.assertFalse(marker.exists())

    def test_model_cannot_read_supervisor_key_or_write_receipt_fd(self):
        code = """import os
from pathlib import Path
for target in ('mem', 'environ', 'fd/1'):
    try:
        fd = os.open('/proc/%d/%s' % (os.getppid(), target), os.O_RDWR)
    except PermissionError:
        continue
    else:
        os.close(fd)
        raise RuntimeError('supervisor access unexpectedly allowed')
Path('protected').write_text('yes')
"""
        status = self.finish(self.launch(code))
        self.assertEqual(status['agent_return_code'], 0)
        self.assertTrue(status['exported'])
        self.assertEqual((self.repo / 'protected').read_text(), 'yes')

    def test_unknown_writer_shutdown_never_exports(self):
        prefix = '''def stop_children(deadline):
    raise TimeoutError("fixture unknown cleanup")'''
        status = self.finish(self.launch("pass", prefix))
        self.assertFalse(status['exported'])
        self.assertFalse(status['writer_stopped'])
        self.assertFalse((self.root / 'artifacts/model.patch').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
