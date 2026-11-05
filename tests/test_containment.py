import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

import probe


class ContainmentTests(unittest.TestCase):
    def test_windows_secret_transport_restores_key_lines(self):
        expected = "-----BEGIN OPENSSH PRIVATE KEY-----\nYWJj\n-----END OPENSSH PRIVATE KEY-----\n"
        self.assertEqual(probe.normalize_deploy_key(expected.replace("\n", "\r\n")), expected)
        self.assertEqual(probe.normalize_deploy_key(expected.rstrip()), expected)
        with self.assertRaises(probe.ProbeFailure):
            probe.normalize_deploy_key("not a credential")

    def test_failed_child_output_stays_in_private_log(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "private.log"
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                with self.assertRaises(probe.ProbeFailure) as caught:
                    probe.run_private([sys.executable, "-c",
                        "import sys; print('test-canary-stdout'); print('test-canary-stderr', file=sys.stderr); sys.exit(3)"],
                        cwd=directory, log=log)
            self.assertEqual(output.getvalue(), "")
            self.assertNotIn("canary", str(caught.exception))
            self.assertIn("test-canary-stdout", log.read_text())
            self.assertIn("test-canary-stderr", log.read_text())

    def test_unexpected_exception_is_not_published(self):
        output = io.StringIO()
        with mock.patch("probe.execute", side_effect=RuntimeError("test-canary-secret")):
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                self.assertEqual(probe.main(), 1)
        self.assertNotIn("test-canary-secret", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())

    def test_revision_rejects_unpinned_or_option_values(self):
        for value in ["main", "--upload-pack=other", "0" * 39, "0" * 40 + "\n"]:
            with self.subTest(value=value), self.assertRaises(probe.ProbeFailure):
                probe.validate_revision(value)
        self.assertEqual(probe.validate_revision("a" * 40), "a" * 40)

    def test_remote_rejects_other_hosts_and_commands(self):
        for value in ["git@other.example:o/r.git", "-oProxyCommand=anything", "git@github.com:o/r.git;echo nope"]:
            with self.subTest(value=value), self.assertRaises(probe.ProbeFailure):
                probe.validate_remote(value)
        self.assertEqual(probe.validate_remote("git@github.com:owner/fixture.git"), "git@github.com:owner/fixture.git")

    def test_ipa_contains_only_app_members(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "Probe.app"
            app.mkdir()
            (app / "Probe").write_bytes(b"sample-executable")
            (root / "outside-private.log").write_bytes(b"must-stay-outside")
            ipa = root / "sample.ipa"
            probe.pack_app(app, ipa)
            with zipfile.ZipFile(ipa) as archive:
                self.assertEqual(archive.namelist(), ["Payload/Probe.app/Probe"])
                self.assertIsNone(archive.testzip())


if __name__ == "__main__":
    unittest.main()
