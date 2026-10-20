"""The repository's scripts, loaded as modules.

None of the three scripts is importable by name: the daemon has no `.py` suffix, and
the endpoint and the filter are shipped as files the image copies into place. Loading
them writes no bytecode, so a test run leaves nothing beside a file the image carries.
"""
import importlib.machinery
import importlib.util
import pathlib
import sys

sys.dont_write_bytecode = True

ROOT = pathlib.Path(__file__).resolve().parents[1]
KIT_BIN = ROOT / "kit" / "files" / "home" / ".local" / "bin"


def load(name, path):
    """The module `path` defines, registered as `name`."""
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


relayd = load("junie_ide_relayd", ROOT / "bin" / "junie-ide-relayd")
endpoint = load("junie_ide_relay_endpoint", KIT_BIN / "junie-ide-relay-endpoint.py")
tty_filter = load("junie_tty_filter", KIT_BIN / "junie-tty-filter.py")
