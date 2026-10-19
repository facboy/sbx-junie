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

### 5. `junie-sbx.sh` does not survive a fresh state directory

`init_junie_home` links into `$JUNIE_HOME` and copies
`${DOT_JUNIE_RO}/sessions/index.jsonl` unconditionally. This host's `~/.junie` has no
`sessions/` directory; the copy then fails and `set -e` aborts with the three links already
made and `sessions/` already created, so the next run finds the directory present, skips
`init_junie_home` entirely, and leaves a Junie home with an empty `sessions/` and no index.
Reproduced against a fixture. The README's "re-running is a no-op" does not cover this.
Because the init runs only when the directory is absent, a session directory created after
setup is never linked either.

The `read -p … (y/N)` prompt is a hard stop for anything unattended, with no flag or
`JUNIE_SBX_YES` to opt in. The rest of the repo is written for scripted use.

### 6. `USER_CONFIG_HOME` is exported and never read

`junie-sbx.sh` passes it; nothing in the kit uses it. Either use it or drop it from
`ENVS`.

### 7. The relay's `grep "^$digest\t"` is right only by accident

This looked like a bug and is not: the source holds a literal tab in the pattern, which I
checked byte by byte and by running the pass under `dash -x` — the record is kept and the
endpoint survives. But it depends on an invisible byte. A formatter, an editor's trailing-
whitespace pass, or a copy through a JSON string turns `\t` into a literal `t`, and the
cleanup loop then kills every live endpoint on every tick. A `grep -F` against a variable
holding the tab, or a comment naming it, is what makes the intent readable.

### 8. Unguarded failure paths in `bin/junie-ide-relayd`

Each of these kills a connection thread that serves every sandbox:

- `refused_tool` assumed `params` and `arguments` were dicts. `{"params": []}` or
  `{"arguments": "ls"}` raised `AttributeError` inside the request thread, which left the
  client socket open. Reproduced, and fixed with item 1: both are type-checked now, and a
  call shaped otherwise than the schema says is refused.
- `authorized` decodes the presented secret with `errors="replace"` but compares against
  the stored one, so a record whose secret is non-ASCII parses in `read_secrets` and then
  makes `hmac.compare_digest` raise `TypeError`. Reproduced.
- `main` binds the listener without catching `OSError`, so a port already in use is a raw
  traceback rather than a message.
- `read_preamble` reads up to 256 bytes one at a time with no per-byte timeout, so after
  `accept` any local process can hold a thread by stalling mid-preamble.

### 9. `splice` re-raises a write error

`peers[source].sendall` sits outside the `try` that guards `recv`, so a peer closing its
read side raises `BrokenPipeError` out of `splice_to`/`relay_rpc` (reproduced). The
`finally` closes the sockets, so nothing leaks, but a routine disconnect writes a traceback
into the log.

## Low severity

- `junie-ide-relay.sh` writes the watcher pidfile and never removes it, so a `SIGTERM`
  leaves a stale pid and the "one watcher per sandbox" check leans on pid reuse — the exact
  class of bug the record-reuse code elsewhere in the same file handles carefully.
- `--secret` on the endpoint defaults to `$JUNIE_IDE_RELAY_SECRET` and nothing else, since
  the launcher never passes the flag. It is dead weight as it stands.
- `docs/fragmented-input-sequence.md` says pieces up to 60 ms apart are rejoined;
  `HOLD_TIMEOUT` is 0.05 s.
- `kit/Dockerfile` pins Playwright 1.63.0, while `kit/README.md` gives advice anchored to
  1.61. Not contradictory, but the two are drifting.
- A `mounts` entry that is only `:ro` reduces to an empty path and is reported as "no host
  path", which is true but not the setting that was wrong. The same unconditional strip
  makes a real directory whose name ends in `:ro` unmountable, since the suffix is removed
  whether or not the shortened path exists. Checking the full entry first and only
  stripping when that path is absent would cover both.
- `junie-init.sh` appends to `~/.npmrc` only when the key is absent, so a stale
  `store-dir` from an earlier sandbox is never corrected.
- `kit/README.md` and `kit/spec.yaml` both give the `1003h` → `1002h` rewrite without the
  `1003l` half, which the module docstring does explain.

## Nits

- `bin/junie-ide-relayd` is 0644 under a `python3` shebang. Nothing invokes it directly
  today — the unit spells out `python3 path` — so the failure is latent.
- `junie-sbx.sh` pins the provider to `ollama`, which leaves the kit's six credential
  declarations unreachable from the shipped entry point.
- `sbx.example.toml` indents `mounts` by four spaces where the rest of the repo uses two.
- The sample unit has no `RestartPreventExitStatus`, so a deliberately refused start
  retries every two seconds and repeats the same `chmod` advice.
- No tests. `bin/junie-ide-relayd` is roughly 1170 lines of parsers with a security
  boundary riding on them, and `split_digest_secret`, `read_chunked`,
  `filtered_response`, `refused_tool` and `scrub_payload` are pure functions that need no
  fixtures. Tests there would have caught every item in the first section.

## Priority

1. ~~The three allowlist holes and the stream lock~~ — done in `bin/junie-ide-relayd`.
2. ~~The exit status and stdin EOF in `junie-tty-filter.py`~~ — done.
3. ~~The missing bindfs presence check in `setup-junie-sbx.sh`~~ — done. The idempotent,
   non-interactive home init in `junie-sbx.sh` remains.
4. Per-connection error handling in the daemon: the non-ASCII secret, bind failure, a
   preamble read timeout.
5. Unit tests for the relay's parsers and the filter's `split_sequences`; then the version
   and doc drift.
