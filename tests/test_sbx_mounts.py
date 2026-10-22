"""`junie-sbx.sh` confines a project's `mounts` entries.

The project's `.junie/sbx.toml` is agent-writable, so an entry it names is a
request rather than a grant: one naming `/`, the home directory or an ancestor
of the project would hand the sandbox the host's tree on the next run, and the
operator who starts that run is the one who approves it.
"""
import os
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "junie-sbx.sh"


class MountsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        self.home = base / "home"
        self.project = self.home / "git" / "project"
        self.project.mkdir(parents=True)
        (self.home / "data").mkdir()

        # `junie-sbx.sh` refuses to run against an empty read-only view.
        view = base / "config" / "junie-sbx" / "junie_ro" / "_junie"
        view.mkdir(parents=True)
        (view / "AGENTS.md").write_text("stub\n")

        bindir = base / "bin"
        bindir.mkdir()
        stub = bindir / "sbx"
        stub.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" > "$SBX_ARGV"\n')
        stub.chmod(0o755)

        self.argv = base / "argv"
        self.env = {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(base / "config"),
            "PATH": "%s:/usr/bin:/bin" % bindir,
            "SBX_ARGV": str(self.argv),
            "JUNIE_SBX_UV": "0",
            "JUNIE_SBX_PNPM": "0",
        }

    def run_with(self, mounts):
        entry = ", ".join('"%s"' % mount for mount in mounts)
        (self.project / ".junie").mkdir(exist_ok=True)
        (self.project / ".junie" / "sbx.toml").write_text("mounts = [%s]\n" % entry)
        return subprocess.run(
            ["bash", str(SCRIPT)],
            cwd=self.project,
            env=self.env,
            capture_output=True,
            text=True,
        )

    def passed_mounts(self):
        return self.argv.read_text().splitlines()

    def test_host_root_is_refused(self):
        done = self.run_with(["/"])
        self.assertNotEqual(0, done.returncode)
        self.assertIn("/", done.stderr)
        self.assertFalse(self.argv.exists())

    def test_home_is_refused(self):
        done = self.run_with(["~"])
        self.assertNotEqual(0, done.returncode)
        self.assertIn("home", done.stderr)
        self.assertFalse(self.argv.exists())

    def test_project_ancestor_is_refused(self):
        done = self.run_with(["~/git"])
        self.assertNotEqual(0, done.returncode)
        self.assertIn("git", done.stderr)
        self.assertFalse(self.argv.exists())

    def test_path_outside_both_roots_is_refused(self):
        done = self.run_with(["/etc"])
        self.assertNotEqual(0, done.returncode)
        self.assertIn("/etc", done.stderr)
        self.assertFalse(self.argv.exists())

    def test_symlink_out_of_the_home_is_refused(self):
        (self.home / "escape").symlink_to("/etc")
        done = self.run_with(["~/escape"])
        self.assertNotEqual(0, done.returncode)
        self.assertIn("escape", done.stderr)
        self.assertFalse(self.argv.exists())

    def test_a_path_under_the_home_is_mounted(self):
        done = self.run_with(["~/data"])
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn(str(self.home / "data"), self.passed_mounts())

    def test_relay_secrets_is_refused(self):
        secrets = self.home.parent / "config" / "junie-sbx" / "relay-secrets"
        secrets.mkdir(parents=True)
        done = self.run_with([str(secrets)])
        self.assertNotEqual(0, done.returncode)
        self.assertIn("relay", done.stderr)
        self.assertFalse(self.argv.exists())

    def test_a_path_under_the_project_is_mounted_read_only(self):
        (self.project / "vendor").mkdir()
        done = self.run_with(["vendor:ro"])
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("%s:ro" % (self.project / "vendor"), self.passed_mounts())
