"""`junie-sbx.sh` asks the operator before mounting a path outside the project.

`.junie/sbx.toml` sits in the project, which the agent can write, so a `mounts`
entry in it is a request rather than a grant. A path under the project is
already the workspace and needs nothing; one outside it is mounted only after
the operator has approved it once, and the approval is recorded beside the relay
secrets, which the sandbox never sees.
"""
import json
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
        self.base = base
        self.home = base / "home"
        self.project = self.home / "git" / "project"
        self.project.mkdir(parents=True)
        (self.home / "data").mkdir()
        self.config = base / "config"

        # `junie-sbx.sh` refuses to run against an empty read-only view.
        view = self.config / "junie-sbx" / "junie_ro" / "_junie"
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
            "XDG_CONFIG_HOME": str(self.config),
            "PATH": "%s:/usr/bin:/bin" % bindir,
            "SBX_ARGV": str(self.argv),
            "JUNIE_SBX_UV": "0",
            "JUNIE_SBX_PNPM": "0",
        }
        (self.project / ".junie").mkdir()

    def run_with(self, mounts, approve=None):
        entry = ", ".join('"%s"' % mount for mount in mounts)
        (self.project / ".junie" / "sbx.toml").write_text("mounts = [%s]\n" % entry)
        env = dict(self.env)
        if approve is not None:
            env["JUNIE_SBX_APPROVE_MOUNTS"] = approve
        return subprocess.run(
            ["bash", str(SCRIPT)],
            cwd=self.project,
            env=env,
            capture_output=True,
            text=True,
        )

    def passed_mounts(self):
        return self.argv.read_text().splitlines()

    def approvals(self):
        record = self.config / "junie-sbx" / "mount-approvals"
        files = list(record.glob("*.json"))
        self.assertEqual(1, len(files))
        return record, files[0]

    def test_a_path_under_the_project_needs_no_approval(self):
        (self.project / "vendor").mkdir()
        done = self.run_with(["vendor:ro"])
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("%s:ro" % (self.project / "vendor"), self.passed_mounts())

    def test_a_path_outside_the_project_is_refused_at_first(self):
        done = self.run_with(["~/data"])
        self.assertNotEqual(0, done.returncode)
        self.assertIn("not approved", done.stderr)
        self.assertIn("JUNIE_SBX_APPROVE_MOUNTS=%s" % (self.home / "data"), done.stderr)
        self.assertFalse(self.argv.exists())

    def test_approval_mounts_the_path_and_is_recorded(self):
        done = self.run_with(["~/data"], approve=str(self.home / "data"))
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn(str(self.home / "data"), self.passed_mounts())
        directory, record = self.approvals()
        self.assertEqual(0o700, directory.stat().st_mode & 0o777)
        self.assertEqual(0o600, record.stat().st_mode & 0o777)
        self.assertEqual([str(self.home / "data")], json.loads(record.read_text())["mounts"])

    def test_a_recorded_approval_outlives_the_run_that_made_it(self):
        first = self.run_with(["~/data"], approve=str(self.home / "data"))
        self.assertEqual(0, first.returncode, first.stderr)
        self.argv.unlink()
        second = self.run_with(["~/data"])
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertIn(str(self.home / "data"), self.passed_mounts())

    def test_an_approval_belongs_to_one_project(self):
        self.assertEqual(0, self.run_with(["~/data"], approve=str(self.home / "data")).returncode)
        other = self.home / "git" / "other"
        (other / ".junie").mkdir(parents=True)
        (other / ".junie" / "sbx.toml").write_text('mounts = ["%s"]\n' % (self.home / "data"))
        done = subprocess.run(
            ["bash", str(SCRIPT)], cwd=other, env=self.env, capture_output=True, text=True
        )
        self.assertNotEqual(0, done.returncode)
        self.assertIn("not approved", done.stderr)

    def test_a_path_outside_the_home_can_be_approved(self):
        shared = self.base / "srv" / "datasets"
        shared.mkdir(parents=True)
        done = self.run_with([str(shared)], approve=str(shared))
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn(str(shared), self.passed_mounts())

    def test_the_host_root_is_refused_because_it_holds_the_relay_secrets(self):
        done = self.run_with(["/"], approve="/")
        self.assertNotEqual(0, done.returncode)
        self.assertIn("relay secrets", done.stderr)
        self.assertFalse(self.argv.exists())

    def test_a_symlink_out_of_the_project_is_judged_by_its_target(self):
        (self.project / "escape").symlink_to("/etc")
        done = self.run_with(["escape"])
        self.assertNotEqual(0, done.returncode)
        self.assertIn("not approved", done.stderr)
        self.assertFalse(self.argv.exists())

    def test_relay_secrets_is_refused_even_when_approved(self):
        secrets = self.config / "junie-sbx" / "relay-secrets"
        secrets.mkdir()
        done = self.run_with([str(secrets)], approve=str(secrets))
        self.assertNotEqual(0, done.returncode)
        self.assertIn("relay secrets", done.stderr)
        self.assertFalse(self.argv.exists())

    def test_a_directory_holding_relay_secrets_is_refused_even_when_approved(self):
        secrets = self.config / "junie-sbx" / "relay-secrets"
        secrets.mkdir()
        parent = secrets.parent
        done = self.run_with([str(parent)], approve=str(parent))
        self.assertNotEqual(0, done.returncode)
        self.assertIn("relay secrets", done.stderr)
        self.assertFalse(self.argv.exists())
