"""`junie-sbx.sh` asks the operator before mounting a path outside the project.

`.junie/sbx.toml` sits in the project, which the agent can write, so a `mounts`
entry in it is a request rather than a grant. A path under the project is
already the workspace and needs nothing; one outside it is mounted only after
the operator has approved it once, and the approval is recorded beside the relay
secrets, which the sandbox never sees.
"""
import json
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

class OverlayTest(unittest.TestCase):
    """A cache tree with no overlay is mounted read-only, never read-write.

    `fuse-overlayfs` can exit 0 and mount nothing, leaving the merged path as the
    host's own cache directory: a sandbox write then lands in the host's
    `~/.cache`, which is the one thing the overlay exists to prevent.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        self.base = base
        self.home = base / "home"
        self.project = self.home / "git" / "project"
        self.project.mkdir(parents=True)
        self.config = base / "config"
        view = self.config / "junie-sbx" / "junie_ro" / "_junie"
        view.mkdir(parents=True)
        (view / "AGENTS.md").write_text("stub\n")

        # The host cache trees the overlays lower onto.
        (self.home / ".cache" / "uv").mkdir(parents=True)
        (self.home / ".local" / "share" / "uv" / "tools").mkdir(parents=True)

        self.bindir = base / "bin"
        self.bindir.mkdir()
        (self.bindir / "sbx").write_text(
            '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$SBX_ARGV"\n')
        (self.bindir / "sbx").chmod(0o755)
        self.argv = base / "argv"
        self.env = {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.config),
            "PATH": "%s:/usr/bin:/bin" % self.bindir,
            "SBX_ARGV": str(self.argv),
            "JUNIE_SBX_UV": "1",
            "JUNIE_SBX_PNPM": "0",
        }
        (self.project / ".junie").mkdir()

    def run_script(self):
        return subprocess.run(
            ["bash", str(SCRIPT)], cwd=self.project, env=self.env,
            capture_output=True, text=True)

    def merged_entries(self):
        """The argv entries mounting a merged tree, which are bare paths.

        The `--env` lines name the same paths, so only an entry with no `=` in it
        is a mount.
        """
        return [line for line in self.argv.read_text().splitlines()
                if "%soverlays%s" % (os.sep, os.sep) in line
                and "=" not in line]

    def test_a_fuse_overlayfs_that_mounts_nothing_makes_the_mount_read_only(self):
        # Exits 0 without mounting, which is the silent failure the check is for.
        stub = self.bindir / "fuse-overlayfs"
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
        done = self.run_script()
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("mounted read-only", done.stderr)
        self.assertIn("fuse-overlayfs", done.stderr)
        entries = self.merged_entries()
        self.assertTrue(entries, "the merged trees are still mounted")
        for entry in entries:
            self.assertTrue(entry.endswith(":ro"), entry)

    def test_a_missing_upper_directory_makes_the_mount_read_only(self):
        stub = self.bindir / "fuse-overlayfs"
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
        # An upper directory the check cannot find is one no overlay used.
        env = dict(self.env)
        done = subprocess.run(
            ["bash", str(SCRIPT)], cwd=self.project, env=env,
            capture_output=True, text=True)
        self.assertEqual(0, done.returncode, done.stderr)
        entries = self.merged_entries()
        self.assertTrue(entries)
        for entry in entries:
            self.assertEqual(":ro", entry[-3:], entry)

    def test_no_overlay_tooling_at_all_makes_the_mount_read_only(self):
        # Neither fuse-overlayfs nor passwordless sudo: the script says so and
        # still mounts each tree read-only.
        sudo = self.bindir / "sudo"
        sudo.write_text("#!/bin/sh\nexit 1\n")
        sudo.chmod(0o755)
        path = self.env["PATH"].replace("/usr/bin:/bin", "/usr/bin:/bin")
        env = dict(self.env)
        env["PATH"] = "%s%s" % (self.bindir, ":/bin")
        done = subprocess.run(
            ["bash", str(SCRIPT)], cwd=self.project, env=env,
            capture_output=True, text=True)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("cannot overlay", done.stderr)
        for entry in self.merged_entries():
            self.assertTrue(entry.endswith(":ro"), entry)

class SessionViewTest(unittest.TestCase):
    """The sandbox sees this project's sessions, not every project's.

    `~/.junie` holds every project's transcripts, so linking all of them leaks
    another project's history into a sandbox that holds one project. `index.jsonl`
    maps a session to the project that recorded it, and only this project's are
    linked.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        self.base = base
        self.home = base / "home"
        self.project = self.home / "git" / "project"
        self.project.mkdir(parents=True)
        self.other = self.home / "git" / "other"
        self.other.mkdir(parents=True)
        self.config = base / "config"
        view = self.config / "junie-sbx" / "junie_ro" / "_junie"
        (view / "sessions").mkdir(parents=True)
        (view / "AGENTS.md").write_text("stub\n")
        for name in ("mine", "theirs", "unindexed"):
            (view / "sessions" / ("session-%s" % name)).mkdir()
        (view / "sessions" / "index.jsonl").write_text(
            json.dumps({"sessionId": "session-mine",
                        "projectDir": str(self.project)}) + "\n"
            + json.dumps({"sessionId": "session-theirs",
                          "projectDir": str(self.other)}) + "\n")

        bindir = base / "bin"
        bindir.mkdir()
        (bindir / "sbx").write_text(
            '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$SBX_ARGV"\n')
        (bindir / "sbx").chmod(0o755)
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

    def run_script(self):
        return subprocess.run(
            ["bash", str(SCRIPT)], cwd=self.project, env=self.env,
            capture_output=True, text=True)

    def home_sessions(self):
        home = (self.config / "junie-sbx" / "sbx-state").glob("*/_junie/sessions")
        found = set()
        for directory in home:
            for entry in directory.iterdir():
                found.add(entry.name)
        return found

    def test_only_this_projects_session_is_linked(self):
        done = self.run_script()
        self.assertEqual(0, done.returncode, done.stderr)
        linked = self.home_sessions()
        self.assertIn("session-mine", linked)
        self.assertNotIn("session-theirs", linked)
        self.assertNotIn("session-unindexed", linked)

    def test_another_projects_transcript_is_not_reachable(self):
        self.assertEqual(0, self.run_script().returncode)
        for directory in (self.config / "junie-sbx" / "sbx-state").glob("*/_junie/sessions"):
            self.assertFalse((directory / "session-theirs").exists())


class OverlayInForceTest(unittest.TestCase):
    """The overlay check accepts a real overlay and rejects everything else.

    It guards a mount decision, so a false positive hands out a writable path over
    the lower tree, and a false negative costs a working host its cache reuse.
    """

    SCRIPT_TEXT = SCRIPT.read_text()

    def run_check(self, merged, base_upper_exists=True, mountinfo=None):
        """Call `overlay_in_force` with `mountinfo` standing in for the real one."""
        import subprocess

        helper = "overlay_in_force() {" + self.SCRIPT_TEXT.split("overlay_in_force() {", 1)[1].split(
            "\n}\n", 1)[0] + "\n}"
        # Point the function at a fixture mountinfo instead of /proc/self/mountinfo.
        helper = helper.replace("/proc/self/mountinfo", str(self.mountinfo_path))
        base = self.base
        if not base_upper_exists:
            os.rmdir(base / "upper")
        script = "%s\noverlay_in_force %s %s\n" % (helper, base, merged)
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True).returncode

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = pathlib.Path(self.tmp.name) / "overlays" / "ms-playwright"
        for name in ("upper", "work", "merged"):
            (self.base / name).mkdir(parents=True)
        self.mountinfo_path = pathlib.Path(self.tmp.name) / "mountinfo"
        self.mountinfo_path.write_text("")

    def entry(self, target, options, fstype="overlay"):
        """One mountinfo line, as the kernel writes it.

        `fuse-overlayfs` fills the two fields after the separator differently --
        `fuse.fuse-overlayfs` and `none` -- and its options are not in the line.
        """
        return "76 112 0:63 / %s rw,relatime - %s %s rw,%s\n" % (
            target, fstype, "none" if fstype.startswith("fuse") else fstype, options)

    def test_a_real_overlay_using_this_upper_is_in_force(self):
        merged = str(self.base / "merged")
        options = "lowerdir=/l,upperdir=%s,workdir=%s" % (self.base / "upper", self.base / "work")
        self.mountinfo_path.write_text(self.entry(merged, options))
        self.assertEqual(0, self.run_check(merged))

    def test_a_trailing_separator_still_matches(self):
        # mountinfo names the mount point without one, so the check strips it.
        merged = str(self.base / "merged")
        options = "lowerdir=/l,upperdir=%s,workdir=%s" % (self.base / "upper", self.base / "work")
        self.mountinfo_path.write_text(self.entry(merged, options))
        self.assertEqual(0, self.run_check(merged + "/"))

    def test_no_mount_at_all_is_not_in_force(self):
        self.mountinfo_path.write_text("")
        self.assertNotEqual(0, self.run_check(str(self.base / "merged")))

    def test_an_overlay_over_another_upper_is_not_in_force(self):
        merged = str(self.base / "merged")
        self.mountinfo_path.write_text(self.entry(merged, "lowerdir=/l,upperdir=/elsewhere,workdir=/w"))
        self.assertNotEqual(0, self.run_check(merged))

    def test_a_fuse_overlayfs_mount_is_in_force(self):
        # Its options live in the fuse process, so `upperdir=` is absent from the
        # line. Treating that as "not an overlay" would force a working host --
        # one using fuse-overlayfs -- to read-only.
        merged = str(self.base / "merged")
        self.mountinfo_path.write_text(
            self.entry(merged, "user_id=1000,group_id=1000", fstype="fuse.fuse-overlayfs"))
        self.assertEqual(0, self.run_check(merged))

    def test_a_fuse_overlayfs_mount_with_a_trailing_separator_is_in_force(self):
        merged = str(self.base / "merged")
        self.mountinfo_path.write_text(
            self.entry(merged, "user_id=1000,group_id=1000", fstype="fuse.fuse-overlayfs"))
        self.assertEqual(0, self.run_check(merged + "/"))

    def test_a_plain_fuse_mount_is_not_in_force(self):
        # fuse-overlayfs is the only fuse type that is an overlay; an sshfs or a
        # bind under fuse is the tree itself.
        merged = str(self.base / "merged")
        self.mountinfo_path.write_text(self.entry(merged, "rw", fstype="fuse.sshfs"))
        self.assertNotEqual(0, self.run_check(merged))

    def test_a_virtiofs_mount_is_not_in_force(self):
        merged = str(self.base / "merged")
        self.mountinfo_path.write_text(self.entry(merged, "rw", fstype="virtiofs"))
        self.assertNotEqual(0, self.run_check(merged))

    def test_a_missing_upper_directory_is_not_in_force(self):
        merged = str(self.base / "merged")
        options = "lowerdir=/l,upperdir=%s,workdir=%s" % (self.base / "upper", self.base / "work")
        self.mountinfo_path.write_text(self.entry(merged, options))
        self.assertNotEqual(0, self.run_check(merged, base_upper_exists=False))
