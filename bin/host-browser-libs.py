#!/usr/bin/env python3
"""Copy the host libraries a Playwright browser links against into one directory.

The sandbox boots a plain base image that ships none of Chromium's runtime
libraries, while the browsers themselves come from the host's Playwright cache.
This walks the browser's own dependency closure here, on the host, where those
libraries exist, and copies the result somewhere the sandbox can mount read-only.

The copy is unfiltered: the sandbox decides what to use, by skipping every name
its own image already resolves. That ordering matters because a host library
that shadows the image's own is fatal -- a foreign libc.so.6 or libm.so.6 on the
sandbox's library path makes the image's binaries die before they start
(tini: undefined symbol __nptl_change_stack_perm, version GLIBC_PRIVATE).

Usage: host-browser-libs.py <playwright-cache> <dest-dir>
"""

import os
import shutil
import subprocess
import sys


def find_chrome(browsers):
    """The full browser, not the headless shell: both share a library set."""
    for root, _dirs, files in os.walk(browsers):
        if "headless" in root:
            continue
        if "chrome" in files:
            return os.path.join(root, "chrome")
    return None


def resolve(path):
    """Follow symlinks. The loader matches sonames, and a directory of dangling
    links would resolve nothing: ldd reports .../libfoo.so.1 -> libfoo.so.1.2.3,
    and only the target is a real file."""
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def closure(entry, skip):
    """Breadth-first over ldd, since ldd reports one level at a time."""
    found = {}
    queue, seen = [entry], set()
    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        try:
            out = subprocess.run(
                ["ldd", current],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            ).stdout
        except OSError:
            continue
        for line in out.splitlines():
            if "=>" not in line:
                continue
            path = line.split("=>", 1)[1].strip().split(" ")[0]
            if not path.startswith("/"):
                continue
            real = resolve(path)
            name = os.path.basename(path)
            if name in skip or name in found:
                continue
            if os.path.exists(real):
                found[name] = real
                queue.append(real)
    return found


def replace_contents(dest):
    """Empty dest without removing it. A sandbox mounts this directory
    read-only, and removing it leaves that mount on an unlinked inode, which
    drops every library the sandbox had registered at once."""
    os.makedirs(dest, exist_ok=True)
    for entry in os.listdir(dest):
        path = os.path.join(dest, entry)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                os.remove(path)
            except OSError:
                pass


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: host-browser-libs.py <playwright-cache> <dest-dir>\n")
        return 2

    browsers, dest = argv[1], argv[2]
    if not os.path.isdir(browsers):
        sys.stderr.write("host-browser-libs: no Playwright cache at %s\n" % browsers)
        return 1

    chrome = find_chrome(browsers)
    if chrome is None:
        sys.stderr.write(
            "host-browser-libs: no chromium under %s; run `npx playwright install chromium` on the host, "
            "or the sandbox will have browsers to drive but nothing to drive them with\n" % browsers
        )
        return 1

    # The loader and the C runtime are one family: taking any of them from the
    # host rather than the image breaks the image. Skipped here as well as in
    # the sandbox, so the mount never carries them in the first place.
    skip = {
        "ld-linux-x86-64.so.2",
        "ld-linux-aarch64.so.1",
        "ld-linux.so.2",
        "libc.so.6",
        "libm.so.6",
        "libpthread.so.0",
        "libdl.so.2",
        "librt.so.1",
        "libresolv.so.2",
        "libutil.so.1",
        "libnsl.so.1",
        "libanl.so.1",
        "libmvec.so.1",
        "libBrokenLocale.so.1",
        "libc_malloc_debug.so.0",
        "libthread_db.so.1",
    }

    libs = closure(chrome, skip)

    # A Chromium that links nothing outside the C runtime is not a Chromium:
    # this is the shape a mistyped cache path or a partially-removed build
    # produces, and mounting it would leave the sandbox with browsers that
    # cannot start, which is the failure this whole path exists to prevent.
    if "libglib-2.0.so.0" not in libs:
        sys.stderr.write(
            "host-browser-libs: %s resolves no libglib-2.0.so.0, so the browser's libraries "
            "were not found; is this a complete Playwright browser install?\n" % chrome
        )
        return 1

    # Rewritten every run: the host's browser build changes under it, and a
    # stale file left from an earlier one would be a library the browser no
    # longer matches. The directory itself stays, since a running sandbox
    # holds it mounted.
    replace_contents(dest)

    for name, source in sorted(libs.items()):
        shutil.copy2(source, os.path.join(dest, name))
    sys.stdout.write("host-browser-libs: %d libraries from %s\n" % (len(libs), chrome))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
