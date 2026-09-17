"""Execute the generated installer against a loopback HTTP server on Linux.

No credentials, model calls or official downloads. Run with unittest directly
in a Linux container to avoid requiring Pier for these shell failure tests.
"""
from __future__ import annotations

import ast
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'src/dradar/pier_grok.py'
TREE = ast.parse(SOURCE.read_text())
NODES = [n for n in TREE.body if
         isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and
         t.id in {'GROK_CLI_VERSION', 'GROK_VERSION_PATTERN', 'GROK_LINUX_SHA256'}
         for t in n.targets) or isinstance(n, ast.FunctionDef) and n.name == '_install_command']
NS = {}
exec(compile(ast.Module(body=NODES, type_ignores=[]), str(SOURCE), 'exec'), NS)
PAYLOAD = b'#!/bin/sh\necho "grok 1.0.13 (release)"\n'


@unittest.skipUnless(sys.platform == 'linux', 'installer targets Linux/glibc')
class GrokInstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / 'tools'
        self.bin.mkdir()
        self.tool('apt-get', 'exit 0')  # OS package provisioning is outside fixture.
        self.mode = 'ok'
        self.payload = PAYLOAD
        self.requests = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                owner.requests += 1
                if owner.mode == 'stall':
                    time.sleep(2)
                    return
                if owner.mode == 'http':
                    self.send_error(503)
                    return
                data = owner.payload
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                if owner.mode == 'truncate' or (owner.mode == 'recover' and owner.requests == 1):
                    self.wfile.write(data[:7])
                    self.wfile.flush()
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                else:
                    self.wfile.write(data)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.target_dir = self.root / 'runtime/bin'
        self.target_dir.mkdir(parents=True)
        self.target = self.target_dir / 'grok'
        self.target.write_text('previous validated installation')

    def tool(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/sh\n' + body + '\n')
        path.chmod(0o755)

    def run_install(self, *, bad_hash=False, fast=False):
        command = NS['_install_command']()
        digest = hashlib.sha256(self.payload).hexdigest()
        for sha in NS['GROK_LINUX_SHA256'].values():
            command = command.replace(sha, '0' * 64 if bad_hash else digest)
        command = command.replace('/opt/grok-runtime', str(self.root / 'runtime'))
        command = command.replace('https://storage.googleapis.com/grok-build-public-artifacts/cli/',
                                  f'http://127.0.0.1:{self.server.server_port}/')
        if fast:
            command = command.replace('--connect-timeout 15 --max-time 120',
                                      '--connect-timeout 0.2 --max-time 0.3')
            command = command.replace('sleep 2;', 'sleep 0.1;')
            command = command.replace('--kill-after=5s 15s', '--kill-after=0.2s 0.3s')
        start = time.monotonic()
        result = subprocess.run(['bash', '-c', command], text=True, capture_output=True,
                                timeout=25, env=dict(os.environ, PATH=f'{self.bin}:/usr/bin:/bin',
                                                     NO_PROXY='*', no_proxy='*'))
        self.elapsed = time.monotonic() - start
        self.assertEqual(list(self.target_dir.glob('.grok.*')), [], result.stderr)
        return result

    def assert_preserved(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.target.read_text(), 'previous validated installation')

    def test_normal(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.target.read_bytes(), PAYLOAD)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.requests, 1)

    def test_truncated_then_recovered(self):
        self.mode = 'recover'
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('curl: (18)', result.stderr)
        self.assertEqual(self.target.read_bytes(), PAYLOAD)
        self.assertEqual(self.requests, 2)

    def test_permanent_truncation(self):
        self.mode = 'truncate'
        result = self.run_install()
        self.assert_preserved(result)
        self.assertEqual(self.requests, 3)
        self.assertIn('exhausted 3 attempts', result.stderr)

    def test_http_failure(self):
        self.mode = 'http'
        self.assert_preserved(self.run_install(fast=True))
        self.assertEqual(self.requests, 3)

    def test_wrong_hash(self):
        self.assert_preserved(self.run_install(bad_hash=True))
        self.assertEqual(self.requests, 1)

    def test_wrong_version(self):
        self.payload = b'#!/bin/sh\necho "grok 1.0.12"\n'
        self.assert_preserved(self.run_install())
        self.assertEqual(self.requests, 1)

    def test_version_command_failure(self):
        self.payload = b'#!/bin/sh\necho "grok 1.0.13"\nexit 1\n'
        self.assert_preserved(self.run_install())

    def test_version_timeout(self):
        self.payload = b'#!/bin/sh\nsleep 10\n'
        self.assert_preserved(self.run_install(fast=True))
        self.assertLess(self.elapsed, 2)

    def test_download_time_budget(self):
        self.mode = 'stall'
        self.assert_preserved(self.run_install(fast=True))
        self.assertEqual(self.requests, 3)
        self.assertLess(self.elapsed, 2.5)

    def test_install_failure_cleanup(self):
        self.tool('mv', 'exit 73')
        self.assert_preserved(self.run_install())

    def test_target_directory_is_not_accepted(self):
        self.target.unlink()
        self.target.mkdir()
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(self.target.iterdir()), [])


class GrokInstallContractTest(unittest.TestCase):
    def test_production_pins_and_budget(self):
        command = NS['_install_command']()
        self.assertIn('https://storage.googleapis.com/grok-build-public-artifacts/cli/grok-1.0.13-linux-', command)
        for sha in NS['GROK_LINUX_SHA256'].values():
            self.assertIn(sha, command)
        self.assertIn('--connect-timeout 15 --max-time 120', command)
        self.assertIn('"${grok_attempt}" -ge 3', command)
        self.assertIn('sleep 2;', command)
        self.assertNotIn('--insecure', command)
        self.assertNotIn('--retry', command)  # no multiplicative curl retries
        self.assertIn('coreutils', command)


if __name__ == '__main__':
    unittest.main(verbosity=2)
