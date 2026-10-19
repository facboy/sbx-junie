#!/usr/bin/env python3
"""Run the CLI on a pty of its own, and hand it whole input sequences only.

The CLI reads input one escape sequence at a time, with 50 ms for the first byte
and 10 ms for each byte after it. A sequence the terminal hands over in two
pieces leaves the reader stopping part-way: what it took is dropped, and the tail
is inserted as typed text. Seen here as `;20;8M` from a hover report, and as
`13u` from Enter, since the kitty keyboard protocol encodes every key as a
sequence of its own.

So a byte the terminal sends is held until the sequence it belongs to ends, and
the sequence is then written to the CLI in one piece. A hold that sees nothing
more for a moment is released as it stands, which is what a lone Escape needs.

The outer terminal is put in raw mode rather than only partly: `ICRNL` would turn
Enter's CR into a NL, and `IXON` would eat Ctrl-S and Ctrl-Q.

One output sequence is rewritten: `ESC[?1003h` becomes `ESC[?1002h`, and nothing
else is touched. Mouse tracking modes 1000, 1002 and 1003 are one mutually
exclusive setting in the terminal, so 1002 keeps press, release and drag reports
and drops the report per mouse movement -- the stream behind most of the
fragments, and 6628 dispatches that found no handler in 14 minutes of hovering.
The `ESC[?1003l` the CLI sends on exit still turns tracking off.

The CLI needs a pty rather than a pipe: JLine falls back to a dumb terminal when
stdout is not a tty, and then the TUI writes nothing at all.
"""
import fcntl
import os
import pty
import select
import signal
import struct
import sys
import termios
import time
import tty

ALL_MOTION_ON = b"\x1b[?1003h"
DRAG_REPORTING_ON = b"\x1b[?1002h"

# How long a partial sequence waits for its final byte before it is passed on.
HOLD_TIMEOUT = 0.05

PARAMETER_BYTES = range(0x20, 0x40)
FINAL_BYTES = range(0x40, 0x7F)


def set_winsize(fd, rows, cols):
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


def escape_length(data, start):
    """Where the escape sequence at `start` ends, or None while it is partial."""
    following = start + 1
    if following >= len(data):
        return None
    kind = data[following]
    if kind == ord("["):
        index = following + 1
        while index < len(data) and data[index] in PARAMETER_BYTES:
            index += 1
        if index >= len(data):
            return None
        if data[index] not in FINAL_BYTES:
            return following + 1
        return index + 1
    if kind == ord("O"):
        return following + 2 if following + 1 < len(data) else None
    # String sequences run to BEL or ST, and carry anything in between.
    if kind in b"]P^_":
        index = following + 1
        while index < len(data):
            if data[index] == 0x07:
                return index + 1
            if (data[index] == 0x1B and index + 1 < len(data)
                    and data[index + 1] == ord("\\")):
                return index + 2
            index += 1
        return None
    return following + 1


def character_length(data, start):
    """Where the UTF-8 character at `start` ends, or None while it is partial."""
    lead = data[start]
    if lead < 0xE0:
        size = 2
    elif lead < 0xF0:
        size = 3
    else:
        size = 4
    if start + size > len(data):
        return None
    for index in range(start + 1, start + size):
        if not 0x80 <= data[index] <= 0xBF:
            return start + 1
    return start + size


def split_sequences(data):
    """The whole sequences at the start of `data`, and the partial one after."""
    end = 0
    index = 0
    while index < len(data):
        byte = data[index]
        if byte == 0x1B:
            stop = escape_length(data, index)
        elif byte < 0x80:
            stop = index + 1
        else:
            stop = character_length(data, index)
        if stop is None:
            break
        index = stop
        end = stop
    return data[:end], data[end:]


def partial_motion_sequence(data):
    """The trailing bytes that may yet complete an all-motion report."""
    for size in range(min(len(ALL_MOTION_ON) - 1, len(data)), 0, -1):
        if ALL_MOTION_ON.startswith(data[-size:]):
            return data[-size:]
    return b""


def main(argv):
    if not argv:
        sys.stderr.write("junie-tty-filter: no command given\n")
        return 2

    stdin_fd = sys.stdin.fileno()
    try:
        rows, cols, _, _ = struct.unpack(
            "HHHH", fcntl.ioctl(stdin_fd, termios.TIOCGWINSZ, b"\0" * 8))
    except OSError:
        rows, cols = 24, 80

    master, slave = pty.openpty()
    set_winsize(master, rows, cols)
    set_winsize(slave, rows, cols)

    # The child puts this pty into the modes it wants; this only has to hand it
    # every byte the terminal sent, unchanged.
    try:
        saved = termios.tcgetattr(stdin_fd)
        tty.setraw(stdin_fd)
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
            sys.stderr.write("junie-tty-filter: %s\n" % err)
        os._exit(127)

    os.close(slave)
    # Non-blocking so a write the child is not draining yet returns a count
    # rather than stalling the loop.
    os.set_blocking(master, False)

    def on_winch(_signum, _frame):
        try:
            r, c, _, _ = struct.unpack(
                "HHHH", fcntl.ioctl(stdin_fd, termios.TIOCGWINSZ, b"\0" * 8))
            set_winsize(master, r, c)
            os.kill(pid, signal.SIGWINCH)
        except OSError:
            pass

    def on_terminate(_signum, _frame):
        # Raising unwinds through the finally below, so the terminal is restored
        # rather than left raw.
        raise SystemExit(0)

    signal.signal(signal.SIGWINCH, on_winch)
    signal.signal(signal.SIGTERM, on_terminate)
    signal.signal(signal.SIGHUP, on_terminate)

    out = sys.stdout.buffer
    to_child = b""
    partial = b""
    held_output = b""
    # When bytes were last added to a hold of either kind. The deadline is
    # measured from that rather than from the loop's cadence: a child writing
    # continuously keeps the loop busy, and a hold would then never expire.
    held_since = None

    def write_output(data, last=False):
        nonlocal held_output, held_since
        data = held_output + data
        held_output = b"" if last else partial_motion_sequence(data)
        if held_output:
            data = data[:-len(held_output)]
            held_since = time.monotonic()
        if data:
            out.write(data.replace(ALL_MOTION_ON, DRAG_REPORTING_ON))
            out.flush()

    def flush_held_output():
        nonlocal held_output, held_since
        if held_output:
            out.write(held_output)
            out.flush()
            held_output = b""
        held_since = None

    status = 0
    stdin_open = True
    try:
        while True:
            readable = [master] + ([stdin_fd] if stdin_open else [])
            writable = [master] if to_child else []
            if (partial or held_output) and held_since is not None:
                timeout = max(0.0, HOLD_TIMEOUT - (time.monotonic() - held_since))
            else:
                timeout = None
            ready, sendable, _ = select.select(readable, writable, [], timeout)

            if stdin_fd in ready:
                try:
                    data = os.read(stdin_fd, 65536)
                except OSError:
                    data = b""
                if data:
                    whole, partial = split_sequences(partial + data)
                    to_child += whole
                    if partial:
                        held_since = time.monotonic()
                else:
                    # Nothing more comes from this side, so the child must not
                    # wait on a line it will never be given.
                    stdin_open = False
                    to_child += b"\x04"

            if ((partial or held_output) and held_since is not None
                    and time.monotonic() - held_since >= HOLD_TIMEOUT):
                # The rest of the sequence is not coming: pass on what arrived
                # rather than let the CLI's own budget decide.
                if partial:
                    to_child += partial
                    partial = b""
                flush_held_output()

            if sendable:
                try:
                    written = os.write(master, to_child)
                except BlockingIOError:
                    written = 0
                except OSError:
                    break
                to_child = to_child[written:]

            if master in ready:
                try:
                    data = os.read(master, 65536)
                except BlockingIOError:
                    data = b""
                except OSError:
                    break
                if not data:
                    break
                write_output(data)

            done, code = os.waitpid(pid, os.WNOHANG)
            if done:
                status = os.waitstatus_to_exitcode(code)
                while True:
                    try:
                        data = os.read(master, 65536)
                    except (BlockingIOError, OSError):
                        break
                    if not data:
                        break
                    write_output(data)
                write_output(b"", last=True)
                break
    finally:
        if saved is not None:
            try:
                termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved)
            except (OSError, termios.error):
                pass
        os.close(master)
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
