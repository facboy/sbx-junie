"""`setup-junie-sbx.sh` turns clipboard text copy on, once.

A sandbox has no display server, so the runtime supplies `wl-copy`, `xclip` and
`xsel` as shims that post the text to the host bridge; the host gates that write
on `clipboard.textCopy`. With the setting off the bridge answers 403 and Junie
reports a failed copy, hinting at those packages even though the tool it ran is
present, so the setup script turns the setting on and says how to undo it.
"""
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "setup-junie-sbx.sh"

# Answers `settings get` from STUB_CLIPBOARD and records every call.
STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$SBX_LOG"
case "$1 $2" in
  "settings get")
    case "$3" in
      clipboard.textCopy) printf '%s\\n' "${STUB_CLIPBOARD:-false}" ;;
      ssh.agentForwardingEnabled) printf 'false\\n' ;;
    esac
    ;;
  "settings set")
    case "$3" in
      clipboard.textCopy) [ "${STUB_SET_FAILS:-0}" = 1 ] && exit 1 ;;
    esac
    ;;
esac
exit 0
"""


class ClipboardSetupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        self.home = base / "home"
        self.config = base / "config"

        view = self.config / "junie-sbx" / "junie_ro" / "_junie"
        view.mkdir(parents=True)
        (view / "AGENTS.md").write_text("stub\n")

        bindir = base / "bin"
        bindir.mkdir()
        # `mount` reports the view as already up, so the bindfs branch is skipped.
        mount_stub = "#!/bin/sh\n" + "printf '%s\\n' " + str(view) + "\n"
        for name, body in (("sbx", STUB), ("mount", mount_stub)):
            stub = bindir / name
            stub.write_text(body)
            stub.chmod(0o755)
        self.bindir = bindir
        self.log = base / "log"

    def run_setup(self, **extra):
        env = {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.config),
            "PATH": "%s:/usr/bin:/bin" % self.bindir,
            "SBX_LOG": str(self.log),
            "JUNIE_SBX_KEEP_SSH_AGENT": "1",
        }
        env.update(extra)
        return subprocess.run(
            ["bash", str(SCRIPT)], env=env, capture_output=True, text=True
        )

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_copy_is_turned_on_and_the_undo_is_printed(self):
        done = self.run_setup()
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("settings set clipboard.textCopy true", self.calls())
        self.assertIn("sbx settings set clipboard.textCopy false", done.stderr)

    def test_a_setting_already_on_is_left_alone(self):
        done = self.run_setup(STUB_CLIPBOARD="true")
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertNotIn("settings set clipboard.textCopy true", self.calls())
        self.assertNotIn("clipboard", done.stderr)

    def test_the_opt_out_skips_the_step(self):
        done = self.run_setup(JUNIE_SBX_NO_CLIPBOARD="1")
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertNotIn("settings get clipboard.textCopy", self.calls())
        self.assertNotIn("clipboard", done.stderr)

    def test_a_failed_set_names_the_command_that_fixes_it(self):
        done = self.run_setup(STUB_SET_FAILS="1")
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("sbx settings set clipboard.textCopy true", done.stderr)

    def test_no_sbx_on_the_path_is_not_an_error(self):
        (self.bindir / "sbx").unlink()
        done = self.run_setup()
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertNotIn("clipboard", done.stderr)


if __name__ == "__main__":
    unittest.main()
