import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

import build_private


class PrivateBuildTests(unittest.TestCase):
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
