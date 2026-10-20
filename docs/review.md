# Code review

Review of the repository at `682e0a9` (the `sbx-kits-contrib` submodule is upstream and was
not reviewed): every tracked file in the repo proper was read, and the pieces that run
outside a sandbox were exercised — the relay's parsers against socket pairs, the tty
filter against a real pty, the relay script against a stub daemon, and the config reader
against sample `.junie/sbx.toml` files.

## What the repo is

A host-side runner for a Junie sandbox: `setup-junie-sbx.sh` (one-time host prep),
`junie-sbx.sh` (per-project `sbx run`), a `kit/` spec plus Dockerfile, and a two-part IDE
relay (`bin/junie-ide-relayd` on the host, `junie-ide-relay.sh` and
`junie-ide-relay-endpoint.py` in the sandbox). On top of that sits a PTY wrapper
(`junie-tty-filter.py`) that repairs the CLI's fragmented-input bug.

The design is coherent and the prose explains intent rather than restating code. The
layering holds up: the per-project secret, the per-connection digest resolution, the
fail-closed permission checks on the secret records, and the one-fixed-host-port idea are
all sound. Most of what follows is in the relay's MCP parsing, the newest and least
exercised code.

## High severity

### 1. The MCP allowlist is bypassed by three ordinary request shapes — fixed

`forward_requests` inspected a body only when `json.loads` yielded a `dict` carrying a
string `method`, and `refuse` returned early when the request had no `id`. Each shape
below was sent to the real `splice_mcp` over a socket pair, with a fake IDE on the far
side:

| request                                         | before         | now    |
| ----------------------------------------------- | -------------- | ------ |
| `tools/call` in a JSON-RPC batch array           | forwarded      | refused |
| `tools/call` as a notification (no `id`)         | forwarded      | refused |
| `tools/call` with `Transfer-Encoding: chunked`   | forwarded      | refused |

The chunked case was the worst: `read_body` returns the framed bytes (`0\r\n…`), so
`json.loads` failed and the body never reached the check, while `refuse` built a
`Content-Length` reply for a message it never parsed. A plain `Content-Length` request
with an `id` was correctly refused, so the filter worked in the shape it was tested in and
leaked in the other three.

The fix reads every shape before the decision: `read_message` strips the chunk framing and
parses, `tool_calls` returns the calls a message holds whether it is one object or a batch,
and `refuse` no longer depends on `id`, so a notification is answered with a `null` id
rather than forwarded. A batch holding one withheld call is held back whole, since the IDE
must not run a member the list withholds and a batch cannot be answered member by member.
A body that will not parse at all is now answered with a `-32700` instead of being
forwarded unread, and `refused_tool` returns a refusal for a `params` or `arguments` of the
wrong type instead of raising `AttributeError` inside the connection thread.

Verified with a 15-case table over `splice_mcp` (withheld and allowed calls, single,
batched, notification, chunked, dispatcher-targeted, malformed): every withheld shape is
refused and never reaches the IDE, every allowed shape is forwarded byte for byte, and the
same table against the previous revision fails five cases.

### 2. A streamed response leaked the unfiltered tool list and wedged the response side — fixed

`forward_responses` treated an event-stream reply as "pass through and `break`" while
holding `write_lock` for the life of the stream. Reproduced against a fake IDE:

- With the session's `GET /stream` open, a later `tools/list` reply on the same connection
  arrived at the client verbatim, `execute_terminal_command` included. The listing scrub
  was disabled for the whole session, not just for the stream.
- `refuse`'s own reply took the same lock, so a refused call issued while a stream was
  open never answered the client; it waited until the client gave up.

The lock was held across an unbounded loop while both writers only need it for a
`sendall` burst. Separately, an idle stream was torn down after `IDLE_TIMEOUT` with no way
for the CLI to tell that from a dead IDE — and a splice did not end when either peer did,
so a closed client left both threads and the IDE's connection held.

The stream is now forwarded chunk by chunk, so its terminator ends the body and the
connection carries on: the `tools/list` reply after it is framed as a reply and scrubbed,
and the lock is taken per burst, so a refusal reaches the client while the stream is open.
`HttpConnection` reads through a short `select` poll rather than the whole budget, which
lets a caller tell a quiet session from a lost peer; an open stream suspends the idle
budget entirely, since a stream is the session's own channel rather than a lull. A body
that pauses mid-message is retried rather than restarted, so a large reply survives a slow
reader. Teardown ends both directions when the IDE goes away or a half-closed client's
reply never arrives, while a client that only half-closes still receives its reply.

Verified against fake peers: events and a following `tools/list` reply both reach the
client with the withheld tool scrubbed; a refusal arrives while the stream is open and is
never forwarded; the stream's terminator is passed on and the session continues; an open
stream survives a quiet budget while a stream-less session is closed at it; a 1.6 MB reply
is delivered byte for byte to a slow reader; and both directions end when either peer
goes away.

### 3. `junie-tty-filter.py` lost the CLI's exit status and hung on non-tty stdin — fixed

- The loop broke out on the first EOF from the master, before the `waitpid` below it, so
  `status` stayed 0. `junie-tty-filter.py sh -c 'exit 3'` returned 0; with a `printf`
  first it returned 3, which is what showed the status was being lost rather than never
  read. A child killed by a signal also reported 0, so every failure and every signal
  death read as success — which breaks `--task` and any CI use.
- With stdin at EOF and not a tty, `os.read` returned `b""`, `stdin_open` went false, and
  the `\x04` written into a raw pty was not an EOF. `junie-tty-filter.py sh -c 'cat
  >/dev/null; echo done' </dev/null` never finished.

The wrapper now execs the CLI directly when stdin is not a terminal: nothing can fragment
its input there, the pty is what made stdin's end undeliverable, and this path also keeps
the exit status without a wrapper around it. On the pty path the loop reaps the child
before returning, so a non-zero status is reported; a signalled child re-raises the same
signal after the terminal is restored, so the caller sees a signal death rather than an
exit code. When the terminal the wrapper reads goes away, the child now gets `SIGHUP`
instead of a `^D` byte a raw pty would only deliver as typed input.

The same pass made `character_length` end an invalid UTF-8 lead after one byte instead of
waiting out the hold for bytes that cannot form a character.

Verified with a real pty and with stdin on `/dev/null`: `exit 3` reports 3 with and without
output, a `SIGTERM`ed child reports `-15`, a closed terminal ends the child with `SIGHUP`,
`cat >/dev/null` terminates, a missing command reports 127, and the `ESC[?1003h` rewrite
and whole-sequence delivery are unchanged. The previous revision hangs on the `/dev/null`
case and returns 0 for `exit 3`.

### 3b. The terminal filter still leaked fragments, and one write could wedge it — fixed

Two faults survived item 3, both reproduced over a real pty with a fake CLI whose reader
copies the CLI's budgets (50 ms for a sequence's first byte, 10 ms for each byte after).

- The input hold and the output hold shared one deadline, so a motion-report hold that
  expired released an input prefix that still had time left. Worse, the input hold itself was
  50 ms — the same budget the CLI has — so any inter-byte gap past that released the prefix
  and the tail arrived as typed text. `ESC[<35` + `;20;8M` came through as six typed
  characters at gaps of 90 ms and up; `ESC[` + `13u` lost Enter the same way. A `1003h` split
  by 90 ms escaped the `1002h` rewrite for the same reason.
- `write_output` wrote to stdout inline, and stdout was left blocking. A terminal that stopped
  draining — a suspended emulator, a slow link — therefore stopped the whole loop, input
  included: a key pressed while a 300 KB frame was unread reached the CLI only once the
  terminal resumed. A write error on a closed terminal also escaped as a traceback and exit
  120 instead of ending the session.

A started sequence now waits `HOLD_TIMEOUT` (0.25 s) for its end, which is well past the gaps
the pty hop produces; a bare `ESC` waits `ESCAPE_TIMEOUT` (0.05 s), since it is the Escape key
as much as a sequence's start. The motion hold keeps its own deadline. Output is queued and
drained by a thread of its own, so no terminal write runs on the path that reads input; a full
queue stops this process reading the side that feeds it, which is the backpressure the child's
own pty would have given it. A terminal that goes away ends the session: the child is hung up,
and one that catches `SIGHUP` and stays is ended after `HANGUP_GRACE`, so a live wrapper cannot
sit on a terminal nothing reads. The tail queued at exit is flushed before the terminal is put
back — restoring first applied `ONLCR` to it and turned every `\n` into a second `\r`.

Verified over a pty: reports and Enter are rejoined at gaps up to 240 ms and released past
that rather than wedging; a split `1003h` is rewritten and `1003l` still passes through; a key
pressed while a 300 KB frame is unread reaches the CLI at once; 400 KB of output arrives byte
for byte, including through a reader taking 997-byte sips; a closed terminal ends the wrapper
in 4 s even when the child ignores `SIGHUP`; and the exit statuses (`3`, `5`, `127`, a signal
death, and the non-tty path) are unchanged. The previous revision fails the gap, the stalled
terminal and the closed terminal cases.

## Medium severity

### 4. `setup-junie-sbx.sh` never checks for bindfs itself — fixed

The mount test is `mount -t fuse.bindfs | fgrep -q …`, which is the right guard: bindfs
registers as `fuse.bindfs`, and `mount -t` filters on that type, so a view already up is
skipped. What is missing is the other half — nothing checks that `bindfs` exists before
calling it. On a host without it the script died with `bindfs: command not found` and exit
127, right after creating the empty mountpoint, which is the opposite of the treatment
`fuse-overlayfs` gets three lines later (a named prerequisite and a warning). Reproduced
here, where bindfs is not installed.

It also left `${JUNIE_RO}/_junie` created and empty, so the next `junie-sbx.sh` run
mounted an empty directory as the read-only Junie home without complaint.

The prerequisite is now checked before the mountpoint is made, so an absent `bindfs`
stops the script with a message naming it and creating nothing. A `bindfs` that runs
and fails takes the mountpoint back with `rmdir`, so the next run starts from the tree it
left rather than from an already-created directory. `junie-sbx.sh` refuses an empty view
outright, so an unfinished setup is reported instead of being mounted.

Verified with a stubbed `bindfs` and `mount`, since this host has neither: a present
`bindfs` is called once and exits 0, a failing one exits 1 with no `_junie` left, an
absent one exits 1 before `junie_ro` is created at all, an already-mounted view skips the
call entirely, and the previous revision still dies at exit 127 with the empty directory
left behind.

### 5. `junie-sbx.sh` does not survive a fresh state directory — fixed

`init_junie_home` linked into `$JUNIE_HOME` and copied
`${DOT_JUNIE_RO}/sessions/index.jsonl` unconditionally. This host's `~/.junie` has no
`sessions/` directory; the copy then failed and `set -e` aborted with the three links
already made and `sessions/` already created, so the next run found the directory present,
skipped `init_junie_home` entirely, and left a Junie home with an empty `sessions/` and no
index. Reproduced against a fixture. The README's "re-running is a no-op" did not cover
this. Because the init ran only when the directory was absent, a session directory created
after setup was never linked either.

The `read -p … (y/N)` prompt was a hard stop for anything unattended, with no flag or
`JUNIE_SBX_YES` to opt in. The rest of the repo is written for scripted use.

The init now runs on every start and answers nothing: `ln -sfn` per name replaces the links
whether or not an earlier run made them, so there is no prompt and no half-made home to
strand. The index is copied only into a home that has none, since the CLI writes that file
as it runs and re-copying it on a later start would drop the sessions the sandbox had
recorded. The host's session directories are linked with `ln -sfn` under `sessions/` rather than
`sessions/` being a one-time copy, and that runs on every start, so a session the host
records after setup is reachable too. A name the home already holds as a real directory is
the sandbox's own session and is skipped; a link, including a broken one, is replaced. An
absent `sessions/` on the host copies and links nothing and fails nothing.

Verified against fixtures: a fresh state directory on this host — whose `~/.junie` has no
`sessions/` — now starts the sandbox and leaves a home with all three links and an empty
`sessions/`, where the previous revision aborted at the copy after prompting; a rerun writes
nothing to stderr; a host index and two session directories are copied and linked; a session
directory added after setup is linked by the next run; a real directory of the same name
keeps its contents; a broken link to a host session is replaced; and an index the CLI wrote
is left alone. The previous revision needs a `y` on stdin and, given one, still aborts.

### 6. `USER_CONFIG_HOME` is exported and never read — fixed

`junie-sbx.sh` passed it; nothing in the kit used it. Removed from `ENVS`. The value was
`${CONFIG_HOME}` — the `junie-sbx` directory itself rather than the XDG root the name
promises — so leaving it in place would have misled the first script that did read it.

### 7. The relay's `grep "^$digest\t"` is right only by accident — fixed

This looked like a bug and is not: the source held a literal tab in the pattern, which I
checked byte by byte and by running the pass under `dash -x` — the record was kept and the
endpoint survived. But it depended on an invisible byte. A formatter, an editor's trailing-
whitespace pass, or a copy through a JSON string turns `\t` into a literal `t`, and the
cleanup loop then kills every live endpoint on every tick.

The separator is now `tab=$(printf '\t')`, and the two `IFS=` values and the four digest
patterns name it. Nothing in the file's content holds a tab, so a whitespace pass cannot
change what the patterns mean. `grep -F` would not have been enough: dropping the anchor
lets a live digest match anywhere in a project path that ends in it, so the patterns kept
`^` and use the variable instead.

Verified by running the relay's two loops over fixtures in `dash` and `bash`: a recorded
digest is skipped and an unrecorded one kept. Turn a literal tab into a `t` or a space and
the same loops kill a live endpoint, which is the failure this avoids. A project path
containing spaces still reads back as one field, and an `rstrip` pass leaves the file
unchanged.

### 8. Unguarded failure paths in `bin/junie-ide-relayd` — fixed

Each of these killed a connection thread that serves every sandbox:

- `refused_tool` assumed `params` and `arguments` were dicts. `{"params": []}` or
  `{"arguments": "ls"}` raised `AttributeError` inside the request thread, which left the
  client socket open. Reproduced, and fixed with item 1: both are type-checked now, and a
  call shaped otherwise than the schema says is refused.
- `authorized` decoded the presented secret with `errors="replace"` but compared against
  the stored one, so a record whose secret is non-ASCII parsed in `read_secrets` and then
  made `hmac.compare_digest` raise `TypeError`. Reproduced.
- `main` bound the listener without catching `OSError`, so a port already in use was a raw
  traceback rather than a message.
- `read_preamble` read up to 256 bytes one at a time with no timeout, so after `accept` any
  local process could hold a thread by stalling mid-preamble.

The comparison is now between bytes: `read_secrets` skips a secret that is not ASCII,
naming the record once, and `authorized` encodes the stored secret before comparing. The
binding reports the address and the reason and returns 1. `read_preamble` gives the whole
line `PREAMBLE_TIMEOUT` -- one deadline, not one per byte, so a caller that dribbles a byte
at a time is bounded too -- and clears the socket timeout afterwards, since what follows
may be a session meant to sit idle. The accept loop also starts `serve`, which reports an
unexpected failure, closes the socket, and leaves the other threads running.

Verified by loading the daemon as a module: a non-ASCII record is skipped with a message
while the good record beside it still authorises and a wrong secret is still refused; a
connection against the bad record now gets EOF instead of a thread that dies and a client
left waiting; a busy port returns 1 with `cannot listen on 127.0.0.1:<port>`; a stalled
client and one that dribbles a byte every 200 ms each release their thread at the 10 s
budget; and a forced exception inside `handle` is logged as `connection failed` with the
socket closed. A live daemon still answers `health` with its protocol version and rejects a
secretless preamble.

### 9. `splice` re-raises a write error

`peers[source].sendall` sits outside the `try` that guards `recv`, so a peer closing its
read side raises `BrokenPipeError` out of `splice_to`/`relay_rpc` (reproduced). The
`finally` closes the sockets, so nothing leaks, but a routine disconnect writes a traceback
into the log.

## Low severity

1. `junie-ide-relay.sh` wrote the watcher pidfile and never removed it, so a `SIGTERM`
   left a stale pid and the "one watcher per sandbox" check leaned on pid reuse — the exact
   class of bug the record-reuse code elsewhere in the same file handles carefully. The
   check also asked only whether the recorded pid was alive, so any live pid passed it.

   The watcher now removes the file as it exits, and the next start confirms the pid by
   its `--watch` command line, as `is_our_endpoint` confirms an endpoint record. Removing
   it at exit needed the loop's wait to be interruptible: a shell defers a trap until the
   foreground command returns, so `sleep "$interval"` in front of the loop held the exit
   trap for up to a full 10 s interval. The sleep is backgrounded and `wait`ed on instead,
   so `SIGTERM` exits at once.

   Verified against the previous revision with a fixture home directory: a pidfile naming
   a live process that is not a watcher now starts a watcher (before: refused to start),
   `SIGTERM` leaves no pidfile (before: left one), and a second start still finds the
   first and exits. Both `dash` and `bash`, and the pass still runs once per interval.
2. `--secret` on the endpoint defaulted to `$JUNIE_IDE_RELAY_SECRET` and nothing else,
   since the launcher never passes the flag; it was dead weight as it stood. Removed, and
   the environment read moved out of the argument default into the body, which is what the
   preamble already used it for. A secret on a command line is readable by any local
   process through `/proc`, which `is_our_endpoint` itself relies on, so the flag was also
   a trap for whoever wired it up next.
3. `docs/fragmented-input-sequence.md` said pieces up to 60 ms apart are rejoined, where
   `HOLD_TIMEOUT` is 0.05 s. It now says 50 ms and names the constant. Measured through the
   filter on a pty with an escape sequence split in two: rejoined for a gap of 46 and 49 ms,
   released as two reads at 54 ms, with the boundary between them depending on scheduling.
4. `kit/Dockerfile` pins Playwright 1.63.0, while the root `README.md` gave
   `--build-arg PLAYWRIGHT_VERSION=1.61.0` as its example — a version the pin had already left
   behind, so the documented way to move the release named a downgrade. The example no longer
   carries a version: it says `<release>` and points at the Dockerfile's `ARG`, which is the one
   place the pin lives. The `1.61` statements in [Browser libraries](../README.md#browser-libraries)
   stay, since they record when Playwright began picking the 26.04 WebKit bundle rather than what
   this image pins.
5. A `mounts` entry that was only `:ro` reduced to an empty path and was reported as "no host
   path", which was true but not the setting that was wrong. It now says it is only the
   marker, and asks for the host path in front of it.

   The other half does not have the fix the review proposed. The script passes the entry
   through to `sbx`, which strips a trailing `:ro` itself, so checking the full entry first
   changes only which path *this* script validates — the string that reaches `sbx` is the
   same either way, and a directory whose name ends in `:ro` still cannot be named. What the
   script can do is refuse the entry when only the full name exists, since the mount would
   otherwise silently land on the stripped path, and say why. A host that has both the
   `:ro`-named directory and the one without is left alone: the marker reading is the one
   `sbx` takes, so it is the one that works.

   Verified by extracting the parser's heredoc out of the script and running it against
   fixture configs, and by running the whole script with a stub `sbx`: `:ro` alone exits 1
   naming the marker instead of "no host path"; a plain path and a path with the marker both
   still resolve and reach `sbx` unchanged; a missing path, an empty entry, an embedded
   newline and a non-list still exit 1; and the previous revision reports the bare marker as
   "no host path".
6. `junie-init.sh` appended to `~/.npmrc` only when the key was absent, so a stale
   `store-dir` or `cache-dir` from an earlier sandbox was never corrected. The overlay it
   named belongs to another project's state directory and is not mounted here, so pnpm
   kept its store and cache where nothing of this sandbox's was written.

   Each key is now rewritten on every start: the line naming it goes and the current one is
   appended, leaving the rest of the file alone. A key is recognised by a literal prefix
   rather than a pattern, so a path holding regex or glob characters reads as itself and
   `stores-dir` is not taken for `store-dir`.

   Verified by running the script under `dash` against fixture homes: a file holding a stale
   `store-dir` and `cache-dir` beside a registry line and another key ends with the current
   values and the other lines intact, a rerun writes an identical file, a home with no
   `.npmrc` gets one holding both keys, and the previous revision leaves the stale values
   in place.
7. `kit/README.md` and `kit/spec.yaml` gave the `1003h` → `1002h` rewrite without the `1003l`
   half, which the module docstring explains: the CLI's own `1003l` on exit turns tracking off
   rather than leaving 1002 on, because the three modes are one setting. Both now say so, and
   `docs/fragmented-input-sequence.md` says it too.

## Nits

1. `bin/junie-ide-relayd` was 0644 under a `python3` shebang. Nothing invoked it directly —
   the unit spells out `python3 path` — so the failure was latent. It is 0755 now, and
   `./bin/junie-ide-relayd --help` prints its usage rather than `Permission denied`.
2. `junie-sbx.sh` pinned the provider to `ollama` in the `sbx run` line, with no way to
   choose another, so the kit's six credential declarations were unreachable from the shipped
   entry point: whatever key the host exported, the CLI was told to use a local Ollama. The
   pin is still the default, since that is what this host runs, but `JUNIE_SBX_PROVIDER` and
   `JUNIE_SBX_MODEL` now override it. `JUNIE_SBX_PROVIDER=` passes no provider at all, which
   leaves the choice — and so the injected credentials — to the CLI, and the model is pinned
   only for the default provider, since that model name belongs to Ollama. An empty
   `JUNIE_SBX_MODEL=` passes no model.

   Verified with a stub `sbx` that prints its arguments: no variable leaves `--provider ollama`
   and `--model deepseek-v4.1-flash:cloud`; `JUNIE_SBX_PROVIDER=` leaves neither; naming another
   provider drops the model pin unless a model is named too; and `JUNIE_SBX_MODEL=` leaves the
   provider and no model.
3. `sbx.example.toml` indented the `mounts` entries by four spaces where the rest of the repo uses
   two. The entries are indented by two now, and the file still parses to the same settings.
4. The sample unit had no `RestartPreventExitStatus`, so a start the daemon deliberately refused
   — a config file that will not load, or a secret record another local user can reach — was
   retried every two seconds, repeating the same message and the same `chmod` advice. A setting
   that has to change cannot be fixed by a restart.

   A refused start now exits `EX_CONFIG` (78), the sysexits status for exactly that, and the
   sample unit names it in `RestartPreventExitStatus`. A transient failure keeps exiting 1, so a
   port another process holds is still retried. The daemon's own docstring and the root README say
   which statuses mean which.

   Verified by running the daemon: a config naming `allow_tools = "nope"` exits 78, an exposed
   secrets directory exits 78, an exposed record exits 78, and a missing instance directory still
   exits 1. `tests/test_relayd.py` covers the same four in `ExitStatusTest`, and
   `systemd-analyze verify --user` accepts the unit.
5. There were no tests, in a daemon of roughly 1500 lines of parsers with a security
   boundary riding on them. `tests/test_relayd.py` now covers `split_digest_secret`,
   `read_chunked`, `read_message`, `tool_calls`, `allowed_tool`, `refused_tool`,
   `scrub_payload`, `filtered_response`, `decode_chunked` and `rebuild_head`, plus a
   socket-pair table over `splice_mcp`: single, batched, notification, chunked,
   unparseable and dispatcher-targeted calls, the stream's terminator and the reply
   after it, and a refusal while the stream is open. `tests/test_tty_filter.py` covers
   `split_sequences`, `character_length` and `partial_motion_sequence`.
   `python3 -m unittest discover -s tests` runs both, and ten of the cases fail against
   the revision that had the allowlist holes.

## Priority

1. ~~The three allowlist holes and the stream lock~~ — done in `bin/junie-ide-relayd`.
2. ~~The exit status and stdin EOF in `junie-tty-filter.py`~~ — done.
3. ~~The missing bindfs presence check in `setup-junie-sbx.sh`~~ — done. ~~The
   idempotent, non-interactive home init in `junie-sbx.sh`~~ — done.
4. ~~`USER_CONFIG_HOME`~~ — done; dropped from `ENVS`.
5. ~~The relay's invisible-tab `grep`~~ — done; the separator is a variable.
6. ~~Per-connection error handling in the daemon~~ — done; the non-ASCII secret, the bind
   failure, the preamble timeout and a per-connection guard.
7. ~~Unit tests for the relay's parsers and the filter's `split_sequences`~~ — done,
   in `tests/`. Then the version and doc drift.
8. ~~The watcher pidfile~~ — done; it goes at exit and the recorded pid is confirmed by
   its `--watch` command line.
9. ~~The endpoint's unused `--secret` flag~~ — done; the environment is the only source.
10. ~~The 60 ms claim in `docs/fragmented-input-sequence.md`~~ — done; it says the two holds
    and their budgets.
11. ~~The stale Playwright version in the README's build-arg example~~ — done; it names no
    version.
12. ~~The `mounts` entry handling in `junie-sbx.sh`~~ — done; a bare `:ro` is named as the
    marker.
13. ~~The stale `store-dir` in `~/.npmrc`~~ — done; each key is rewritten on every start.
14. ~~The missing `1003l` half of the rewrite~~ — done; both docs say the CLI's own reset still
    turns tracking off.
15. ~~The daemon's missing execute bit~~ — done; it is 0755, so its shebang resolves.
16. ~~The hard-pinned provider~~ — done; `JUNIE_SBX_PROVIDER` and `JUNIE_SBX_MODEL` override
    it, and an empty provider passes none, so the kit's credentials apply.
17. ~~The four-space indent in `sbx.example.toml`~~ — done; the entries are indented by two.
18. ~~The unit's missing `RestartPreventExitStatus`~~ — done; a refused start exits 78 and the
    unit stops retrying it.
19. ~~The filter's shared deadline and its blocking output write~~ — done; the holds have
    separate budgets, and the terminal is fed by a thread so a stalled one cannot wedge
    input.
