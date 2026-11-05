import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import zipfile

import build_private


class PrivateBuildTests(unittest.TestCase):
    def test_interruption_preserves_log_and_reports_no_success(self):
        with tempfile.TemporaryDirectory() as d:
            work = Path(d) / 'retained'
            def cancelled(directory):
                (directory / 'private.log').write_text('private-interruption-canary')
                raise build_private.BuildInterrupted()
            output = io.StringIO()
            with mock.patch.dict(os.environ, PRIVATE_WORK_DIR=str(work)):
                with mock.patch('build_private.execute', side_effect=cancelled):
                    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                        self.assertEqual(build_private.main(), 130)
            self.assertEqual((work / 'private.log').read_text(), 'private-interruption-canary')
            self.assertNotIn('canary', output.getvalue())
            self.assertNotIn('Build passed', output.getvalue())

    def test_recovery_rejects_public_destination_without_upload(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, PRIVATE_BUILD_SPEC=json.dumps({'repository': 'owner/result'}),
                                 PRIVATE_TRANSFER_TOKEN='test-token'):
                with mock.patch('build_private.subprocess.run', return_value=mock.Mock(stdout=b'{"private":false}')) as run:
                    with self.assertRaises(ValueError):
                        build_private.recover(Path(d))
                    self.assertEqual(run.call_count, 1)

    def test_recovery_only_uploads_bounded_selected_logs(self):
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            (work / 'result').mkdir()
            (work / 'result/private.log').write_text('private-diagnostic-canary')
            (work / 'secret').write_text('must-not-upload')
            with mock.patch.dict(os.environ, PRIVATE_BUILD_SPEC=json.dumps({'repository': 'owner/result'}),
                                 PRIVATE_TRANSFER_TOKEN='test-token', GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='2'):
                with mock.patch('build_private.subprocess.run', return_value=mock.Mock(stdout=b'{"private":true}')) as run:
                    build_private.recover(work)
                    self.assertEqual(run.call_count, 2)
                    self.assertIn('private-recovery-123-2', run.call_args.args[0])
            self.assertIn('private-diagnostic-canary', (work / 'recovery.log').read_text())
            self.assertNotIn('must-not-upload', (work / 'recovery.log').read_text())

    @unittest.skipUnless(os.name == 'posix', 'Requires POSIX process groups')
    def test_sigterm_stops_child_and_retains_private_output(self):
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            child = 'import os,time; print("private-signal-canary", os.getpid(), flush=True); time.sleep(60)'
            script = ('import build_private, signal, sys; from pathlib import Path; '
                      'signal.signal(signal.SIGTERM, build_private.interrupt); '
                      '\ntry: build_private.run([sys.executable,"-c",sys.argv[2]], sys.argv[1], Path(sys.argv[1])/"log", None)'
                      '\nexcept build_private.BuildInterrupted: sys.exit(130)')
            process = subprocess.Popen([sys.executable, '-c', script, str(work), child],
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 10
                while not (work / 'log').exists() or not (work / 'log').stat().st_size:
                    if time.monotonic() > deadline:
                        self.fail('Child did not start')
                    time.sleep(0.05)
                process.send_signal(signal.SIGTERM)
                output, _ = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 130)
                self.assertNotIn(b'private-signal-canary', output)
                self.assertIn('private-signal-canary', (work / 'log').read_text())
                child_pid = int((work / 'log').read_text().split()[1])
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()

    def test_source_path_and_symlink_attacks(self):
        for name, mode in [('../outside', 0), ('/absolute', 0), ('C:/outside', 0),
                           ('dir\\outside', 0), ('link', 0o120777)]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                with zipfile.ZipFile(root / 's.zip', 'w') as z:
                    entry = zipfile.ZipInfo(name)
                    entry.filename = name
                    entry.external_attr = mode << 16
                    z.writestr(entry, 'private')
                with self.assertRaises(ValueError):
                    build_private.extract_source(root / 's.zip', root / 'out')

    def test_split_roundtrip_and_hashes(self):
        for size in [0, 1, 10, 11, 20, 31]:
            with self.subTest(size=size), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                data = bytes(range(size))
                (root / 'source').write_bytes(data)
                parts = build_private.split_file(root / 'source', root, limit=10)
                self.assertEqual(b''.join((root / p['name']).read_bytes() for p in parts), data)
                for part in parts:
                    self.assertEqual(part['sha256'], build_private.digest(root / part['name']))
                    self.assertLessEqual(part['bytes'], 10)

    def test_child_failure_and_unexpected_exception_are_private(self):
        with tempfile.TemporaryDirectory() as d:
            output = io.StringIO()
            log = Path(d) / 'private.log'
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                with self.assertRaises(RuntimeError):
                    build_private.run([sys.executable, '-c',
                        'import sys; print("private-negative-control"); sys.exit(1)'], d, log, None)
                with mock.patch('build_private.execute', side_effect=ValueError('private-negative-control')):
                    self.assertEqual(build_private.main(), 1)
            self.assertNotIn('private-negative-control', output.getvalue())
            self.assertIn('private-negative-control', log.read_text())

    def test_result_archive_excludes_sibling_secret(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'secret').write_text('credential')
            result = root / 'result'
            result.mkdir()
            (result / 'report.json').write_text(json.dumps({'status': 'passed'}))
            build_private.pack_result(result, root / 'result.zip')
            with zipfile.ZipFile(root / 'result.zip') as z:
                self.assertEqual(z.namelist(), ['report.json'])
                self.assertIsNone(z.testzip())
