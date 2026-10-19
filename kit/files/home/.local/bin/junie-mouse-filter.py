#!/usr/bin/env python3
"""Run the CLI on a pty and refuse all-motion mouse reporting.

Junie enables `ESC[?1003h` and reads hover reports with a 10 ms per-byte budget.
When the terminal delivers a report in more than one chunk the reader gives up
part-way and the remaining bytes are inserted as typed text -- `;20;8M` in the
prompt. Down-converting that to `ESC[?1002l` leaves click and drag reporting on
and removes the hover stream that trips the reader.

The CLI needs a pty of its own: JLine falls back to a dumb terminal when stdout
is a pipe, and the TUI then writes nothing at all. This allocates one, passes
the terminal size through, and filters only the bytes crossing from the child.
"""
import fcntl
import os
import pty
import select
import signal
import struct
import sys
import termios

ALL_MOTION_ON = b"\x1b[?1003h"
ALL_MOTION_OFF = b"\x1b[?1003l"


def set_winsize(fd, rows, cols):
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


def main(argv):
    if not argv:
        sys.stderr.write("junie-mouse-filter: no command given\n")
        return 2

    try:
        rows, cols, _, _ = struct.unpack(
            "HHHH", fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b"\0" * 8))
    except OSError:
        rows, cols = 24, 80

    master, slave = pty.openpty()
    set_winsize(master, rows, cols)
    set_winsize(slave, rows, cols)

    # Raw on the outer terminal so keystrokes reach the child unmodified; the
    # child sets its own modes on the pty it is given.
    try:
        saved = termios.tcgetattr(sys.stdin.fileno())
        raw = termios.tcgetattr(sys.stdin.fileno())
        raw[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG | termios.IEXTEN)
        raw[1] &= ~termios.OPOST
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, raw)
    except (OSError, termios.error):
        saved = None

    pid = os.fork()
    if pid == 0:
        os.close(master)
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
        for fd in (0, 1, 2):
            os.dup2(slave, fd)
        if slave > 2:
            os.close(slave)
        try:
            os.execvp(argv[0], argv)
        except OSError as err:
            sys.stderr.write("junie-mouse-filter: %s\n" % err)
        os._exit(127)

    os.close(slave)

    def on_winch(_signum, _frame):
        try:
            r, c, _, _ = struct.unpack(
                "HHHH", fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b"\0" * 8))
            set_winsize(master, r, c)
            os.kill(pid, signal.SIGWINCH)
        except OSError:
            pass

    signal.signal(signal.SIGWINCH, on_winch)

    stdin_fd = sys.stdin.fileno()
    out = sys.stdout.buffer
    pending = b""
    status = 0
    try:
        while True:
            readable, _, _ = select.select([stdin_fd, master], [], [])
            if stdin_fd in readable:
                try:
                    data = os.read(stdin_fd, 65536)
                except OSError:
                    data = b""
                if not data:
                    os.write(master, b"\x04")
                else:
                    os.write(master, data)
            if master in readable:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                if not data:
                    break
                # Hold back a partial match so a sequence split across reads is
                # still recognised once its tail arrives.
                data = pending + data
                pending = b""
                for cut in range(len(data), max(0, len(data) - len(ALL_MOTION_ON) + 1), -1):
                    if ALL_MOTION_ON.startswith(data[cut:]):
                        pending = data[cut:]
                        data = data[:cut]
                        break
                out.write(data.replace(ALL_MOTION_ON, ALL_MOTION_OFF))
                out.flush()
            done, code = os.waitpid(pid, os.WNOHANG)
            if done:
                status = os.waitstatus_to_exitcode(code)
                # Drain whatever the child wrote before exiting.
                while True:
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        break
                    if not data:
                        break
                    out.write(data.replace(ALL_MOTION_ON, ALL_MOTION_OFF))
                    out.flush()
                break
    finally:
        if saved is not None:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved)
            except (OSError, termios.error):
                pass
        os.close(master)
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
