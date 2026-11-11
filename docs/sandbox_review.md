# Sandbox security evaluation

Evaluation of a running `junie` sandbox, read at `24ec31f`, on 2026-10-22. Every finding
below was measured from inside the sandbox rather than read off the source. The target was
`sandbox_name=junie-sbx-junie`, uid 1000 `agent`, `groups=agent,sudo,docker`, no effective
capabilities, no seccomp filter, a Docker engine in the sandbox's own VM, and `sudo -n`
configured as `agent ALL=(ALL) NOPASSWD:ALL`.

The distinction that matters throughout: the relay's own boundary is drawn carefully and
holds under test, while the containment the relay is a part of does not, because the largest
grants come from the runtime beneath this repo rather than from the repo.

## Verdict

The per-project relay secret, the fail-closed record permissions, the containment check on
the resolved project path and the MCP tool allowlist are all correct in the shapes tested.
They also defend a boundary that a hostile agent does not have to cross. At `24ec31f` the
sandbox forwarded the host's SSH agent, reached a broader host and internet set than
`kit/spec.yaml` names, and accepted any existing host path as a `mounts` entry from a directory
the agent can write.

`setup-junie-sbx.sh` closes the first of the three
([finding 1](#1-the-hosts-ssh-agent-is-forwarded-in-and-can-authenticate--fixed)), and
`junie-sbx.sh` now requires an operator's approval before it mounts a path from outside the
project, which closes finding 4. The egress finding stays open: sbx's own allow-set widens what
`kit/spec.yaml` names, and nothing in this repo can narrow it.

## Critical

### 1. The host's SSH agent is forwarded in and can authenticate — fixed

The runtime set `SSH_AUTH_SOCK_GATEWAY`, and `/run/ssh-agent.sock` inside the sandbox was that
agent. It was mode `srw-rw-rw-`, so every uid in the sandbox could use it, not only `agent`.

The private keys themselves are absent — `/home/chrisng/.ssh` does not exist in the sandbox —
which is not a mitigation. An agent that signs is the identity for every service that trusts
the key:

```console
$ ssh-add -l
4096 SHA256:6kj7tOP1OtGlgwfyNqdo0t89XlcAtrRifAibr0khV4w /home/chrisng/.ssh/id_rsa (RSA)
 256 SHA256:BqdMWPDHS1s4O09fiY2NWAPnNEUdtLm7nk1ik2tpJNg facboy@gmail.com (ED25519)

$ ssh-keygen -Y sign -f <(ssh-add -L | head -1) -n file msg.txt
-----BEGIN SSH SIGNATURE-----
U1NIU0lHAAAAAQAAAhcAAAAHc3NoLXJzYQAAAAMBAAI…

$ ssh -p 443 git@ssh.github.com
Hi facboy! You've successfully authenticated, but GitHub does not provide shell access.

$ git ls-remote ssh://git@ssh.github.com:443/facboy/sbx-junie.git
24ec31f4e6b853e9112eaecb5a2d623601fbf99e	HEAD
24ec31f4e6b853e9112eaecb5a2d623601fbf99e	refs/heads/main

$ git push --dry-run ssh://git@ssh.github.com:443/facboy/sbx-junie.git \
      HEAD:refs/heads/probe-does-not-exist
 * [new branch]      HEAD -> probe-does-not-exist
```

Anything the agent ran reached this: a `postinstall`, a test harness, a dependency's setup
script, or a prompt-injected instruction. The dry run performed no write; the capability it
demonstrates is a push to any repository the user can push to, from code the user never
reviewed.

This is the widest grant in the sandbox because it is the user's own identity rather than a
resource scoped to one project. `setup-junie-sbx.sh` turns the daemon-level
`ssh.agentForwardingEnabled` off and restarts the daemon, so no sandbox the daemon serves gets
the forward. Removing the socket by hand has the same effect: `ssh-add` and the GitHub
authentication it carried both stop. A sandbox that does need to push wants its own deploy key
for the project it holds.

### 2. Egress reaches hosts `kit/spec.yaml` does not name

`permissions.network.allow` names seven provider hosts and `localhost:27183`.
`kit/README.md` states that `github.com`, `raw.githubusercontent.com` and
`release-assets.githubusercontent.com` are absent from the list and that nothing installs at
runtime. Measured from inside:

| host | result |
| --- | --- |
| `github.com` | `200` |
| `raw.githubusercontent.com/…` | `301`, real file content returned |
| `api.github.com` | `200` |
| `gist.github.com` | `302` |
| `gitlab.com`, `bitbucket.org` | `301`, `200` |
| `pypi.org/simple/` | `200`, real index |
| `registry.npmjs.org/left-pad` | `200`, real metadata |
| `example.com`, `httpbin.org`, `ifconfig.me`, `webhook.site`, `ngrok.io` | `403` |
| `bing.com`, `amazon.com`, `slack.com` | `403` |

Refusal is a real refusal, with a queue behind it:

```console
$ curl -i https://example.com/
HTTP/1.1 403 Forbidden
Approval required for example.com:443.

Review and respond with:
  sbx policy approval ls
```

Two gates enforce it, and both admit more than the spec names. The resolver answers only
allowlisted names — `example.com`, `google.com`, `pastebin.com`, `transfer.sh`, `webhook.site`
and `0x0.st` are NXDOMAIN — while `ssh.github.com`, `codeload.github.com`,
`objects.githubusercontent.com`, `pypi.org` and `registry.npmjs.org` resolve. A raw address
reaches the same conclusion: TLS to `140.82.121.4` with `github.com` as the server name
completes and returns `200`, while `93.184.216.34` with `example.com` fails the handshake.

The consequence is that the agent can fetch and run third-party code from PyPI, npm and
GitHub, and `docker run` pulls from Docker Hub (verified with `alpine`). GitHub stays reachable
over HTTPS after finding 1's fix, which removes the agent push but not the egress.

Some of those hosts are not this kit's grant and not anyone's approval: sbx bakes URLs of its own
into the runtime, and a host in that set answers whatever kit is loaded. `docker run alpine`
succeeded inside the sandbox although `kit/spec.yaml` names no registry host, so Docker Hub was
reachable without this repo naming it. `permissions.network.allow` is added on top of that set,
and on top of the host's policy preset, rather than replacing either, so a kit can neither grant
nor withdraw those hosts. Which measured row came from sbx, from the preset, or from this kit
could not be separated from inside the sandbox, where `sbx policy ls` is unavailable.

So this is a documentation gap rather than a spec that got its own list wrong. "No install host"
holds for `kit/spec.yaml` and says nothing about what the list sits on, which is why the boundary
in force is wider than reading the spec suggests. `deny-all` is the posture where the declared
list is the whole surface; a host that reads this allowlist as exhaustive without applying that
policy is reading a boundary it does not have.

### 3. The host-loopback narrowing is two ports, not one — documented

`kit/README.md` argued that naming one fixed port made `localhost:27183` a narrowing rather
than an opening onto every host port. The relay port is indeed the only one the *kit*
allowlists, and the IDE's ephemeral port is correctly never dialled. A second host service
answers anyway, and nothing else does:

```console
$ # 11434, the host's model server, no preamble needed
GET / → 200 OK, "Ollama is running"

$ # 27183, the relay, which answers one line to a well-formed preamble
JUNIE-RELAY 1 health\n → JUNIE-RELAY 1 ok 3
GET / → connection closed unread; the sandbox proxy reports 500 for that

$ # every other port tried: the IDE's 41832, the MCP mirror at 64562, 3000, 5000, 8080, 9999
curl GET / → 403, "Approval required for localhost:<port>."
bare socket → connection accepted, then closed with no reply
```

The gate is per port rather than per host: an unlisted port is refused — a socket that connects
and is closed unread, which `curl` reports as the `403` and its approval message — and the two
that answer are the two named somewhere in the composition: `27183` by this kit, `11434` by the
runtime so the pinned `ollama` provider works. The IDE's own port and the MCP mirror are
correctly absent from both.

So the narrowing holds where it matters — the sandbox cannot dial an arbitrary host port — while
`kit/README.md`'s "one fixed port" described half of it. The model server is the other half, and
anything in the sandbox can reach it, not only the CLI. It comes from the runtime rather than from
any entry in this repo, so a kit's allowlist cannot withdraw it; `kit/README.md` and the spec's
comment on the allowlist name both ports now.

## High

### 4. `mounts` accepts any existing host path, including `/` — fixed

`junie-sbx.sh` reduced a `mounts` entry to an absolute path and checked three things: it was not
empty, it held no newline, and `os.path.exists`. Nothing confined it to the project, `$HOME`
or any other root, and nothing denied `/`.

Driven against a stub `sbx` with a fixture home:

```console
$ cat .junie/sbx.toml
mounts = ["/", "~"]

$ junie-sbx.sh
  ARG: /
  ARG: /home/operator
```

`~` is the fixture home, expanded to an absolute path; `/` is passed unchanged.

The project directory is agent-writable by design, and `.junie/sbx.toml` sits in it, so this
was a two-run primitive:

1. The agent writes `.junie/sbx.toml` naming `/`, `~`, `~/.ssh:ro`, or
   `~/.config/junie-sbx/relay-secrets`.
2. The next run — one the operator starts normally — mounts it into the sandbox.

`relay-secrets` is the one directory deliberately not mounted, and it is what keeps the
per-project secret meaningful. Naming it would hand one sandbox every project's secret and
with them every project's IDE, which removes the boundary the
[per-project secret](#what-holds) draws.

An entry outside the project now needs an approval the operator records once, and the record
lives beside the relay secrets, which the sandbox never mounts. The run that grants it writes
the path, keyed on the project, under `mount-approvals` there at mode `0700`/`0600`; later runs
read it, so the sandbox cannot add a mount of its own. A path under the project needs nothing,
since that is already the workspace. `os.path.realpath` decides both cases, so a symlink is
judged by where it lands, and its target is what gets approved and mounted.

The one thing no approval reaches is what holds the secrets and the approvals themselves: each of
`relay-secrets` and `mount-approvals`, and any directory containing either, is refused. That is
what makes `/` a refusal rather than an approval an operator might grant while thinking it
harmless, since mounting it would hand the sandbox every project's secret.

Ten cases in `tests/test_sbx_mounts.py` drive the script against a stub `sbx`: a project path
mounts on its own; `~/data` is refused until approved, then mounts and is recorded at `0700`
and `0600`; the record survives the next run; another project does not inherit it; a symlink to
`/etc` is refused; and `relay-secrets`, its parent and `/` are refused even with the approval
set. `README.md` and `sbx.example.toml` describe the approval and name where the record lives.

### 5. The per-project Junie home is a writable host path outside the workspace

`JUNIE_HOME` is mounted read-write:

```
JUNIE_HOME=/home/chrisng/.config/junie-sbx/sbx-state/sbx-junie/_junie
```

That path is on the host and outside the project. Writing to it from the sandbox succeeds.
`sessions/index.jsonl` is `-rw-------` and owned by `agent`, and it is the file
`init_junie_home` seeds and later copies — so the agent can write the record the next run
reads.

The three links (`AGENTS.md`, `docs`, `tools`) are safe, because each resolves into the
read-only view and a write through one fails with `Read-only file system`. The rest of the
directory is agent-controlled host state. The overlay mounts (`UV_CACHE_DIR`, `PNPM_HOME`,
`PLAYWRIGHT_BROWSERS_PATH`, `UV_TOOL_DIR`) are read-write host paths outside the project for
the same reason, by design, and each is a place a compromised agent can leave something the
next run reads.

## Medium

### 6. All of `~/.junie` is exposed read-only, including other projects' sessions

`setup-junie-sbx.sh` bind-mounts the whole of `~/.junie` with `bindfs -r`, and `junie-sbx.sh`
takes the whole view as a workspace. In this evaluation it was 3.8 GB:

| path | size | content |
| --- | --- | --- |
| `sessions/` | 3.3 G | 65 session directories, plus the index |
| `logs/` | 492 M | CLI logs |
| `history.jsonl` | 361 K | prompt history, tagged by project |
| `versions/` | 7.2 M | installed CLI builds |
| `trust/authentication-key` | 43 B | Junie's own trust anchor |

`history.jsonl` is readable and spans every project on the host: 917 prompts for
`/home/chrisng/git/coffee-cards`, 724 for `/mnt/d/git/auto-mask-shader`, 191 for `duplicacy`,
117 for `duplicacy-scripts`, 70 for `/home/chrisng`, 53 for `Reshade-Shaders`, and more.
Transcripts of those projects are readable too, at 855 KB and 581 KB for the two largest
sampled.

The README frames the session links as a convenience so a host session recorded after setup
is reachable. The measured effect is that a sandbox holding project A reads project B's full
transcripts and prompt history, which is the cross-project isolation the relay secret exists
to enforce, leaked through a different door. Narrowing the view to `AGENTS.md`, `docs/`,
`tools/` and the one project's sessions would keep the convenience and drop the leak.

`trust/authentication-key` is readable as `agent` and is 43 bytes of key material.

### 7. The RPC relay splices unfiltered while the MCP relay filters

`relay_rpc` calls `splice_to`, a byte splice with no parsing. Any method reaches the IDE:

```console
POST /rpc {"method":"JunieUnknownService.noSuchMethod"}
→ {"error":{"code":-32601,"message":"Unknown service: JunieUnknownService"},"id":1}

POST /rpc {"method":"JunieMcpService.getBuiltInMcpServerCredentials"}
→ {"result":{"port":64562,"tokenName":"IJ_MCP_AUTH_TOKEN","tokenValue":"…"}}
```

The plugin currently exposes only `JunieMcpService.getBuiltInMcpServerCredentials`, so there
is nothing dangerous to reach today. That is the plugin's current surface rather than a
property of the relay. The MCP channel exists precisely so a tool a later IDE build adds does
not arrive unreviewed; the RPC channel has no equivalent, so a method a later plugin adds is
reachable from the sandbox with no review.

### 8. `sudo NOPASSWD:ALL` and the `docker` group do not break read-only, but they are relied on

`/etc/sudoers.d/agent` holds `agent ALL=(ALL) NOPASSWD:ALL`, and `agent` is in `docker`.

The read-only mounts survive root, which is stronger than the design assumes:

```console
$ sudo -n sh -c "echo pwned > …/junie_ro/_junie/AGENTS.md"
sh: cannot create …/junie_ro/_junie/AGENTS.md: Read-only file system

$ sudo -n mount -o remount,rw …/junie_ro/_junie && sudo -n touch …/junie_ro/_junie/CANARY
touch: cannot touch …/junie_ro/_junie/CANARY: Read-only file system
```

Root does hold the sandbox's VM. `docker run --privileged -v /home/chrisng:/h alpine ls /h`
lists the VM's tree, and the engine pulls arbitrary images.

`junie-sbx.sh` depends on this grant: `merge_overlay` falls back to `sudo -n mount -t overlay`
and `setup-junie-sbx.sh` presents the choice as "install fuse-overlayfs **or** allow
passwordless sudo". `fuse-overlayfs` is the no-root option, and advising passwordless sudo to
avoid installing one package trades the sandbox's privilege boundary for a build step.

### 9. `execute_tool` is checked on its first word

`refused_tool` reads `params.arguments.command`, splits it, and compares `words[0]` against
the allow patterns. A call whose command is `read_file ../../etc/passwd` passes, because the
first word matches `read_*`, and is then dispatched by the IDE with whatever argument
semantics the IDE applies. The filter reasons about a string prefix on a command line it does
not parse, which is a weaker statement than "this tool and these arguments are allowed".

`docs/review.md` records the related open question: `read_*`, `list_*` and `get_*` are
granted, and a `read_*` tool that resolves a host path outside the project would give the
sandbox a host read through the allowlist. That question stayed open here — see
[Not verified](#not-verified).

### 10. The relay secret is long-lived and in the agent's environment

`JUNIE_IDE_RELAY_SECRET` is in `env` for the whole session, and `provision_relay_secret`
deliberately keeps a project's record across runs so a running sandbox is not stranded. The
secret is the only boundary on the shared host loopback port, so any code the agent runs
reads it and inherits the sandbox's IDE reach for as long as the sandbox lives.

Inside the sandbox the secret is not what separates processes from each other: the sandbox's
own endpoint at `127.0.0.1:27183` forwards its preamble with the secret for any local caller.
The secret separates one sandbox from another on the host, which is what it is for.

### 11. A project symlink reaches the host, and the check is lexical — fixed

Found in a second pass. Findings 4, 6, 9 and 10 all rest on a boundary that one symlink
crosses, because the IDE's project-containment check compares the path it was handed rather
than where that path lands, and the project is shared with the host writable.

```console
$ ln -s /home/chrisng/.ssh <project>/.p-ssh

# a plain host path is refused, as designed
read_file  /home/chrisng/.ssh/id_ed25519
  → File /home/chrisng/.ssh/id_ed25519 is outside project, library, and SDK roots

# the same file through the project symlink is read whole
read_file  <project>/.p-ssh/id_ed25519
  → L1: -----BEGIN OPENSSH PRIVATE KEY----- … END OPENSSH PRIVATE KEY
```

The read is the host's, not the sandbox's: `/etc/hostname` is `localhost.localdomain` inside,
and an IDE read through a project symlink to it returned `Jenna3`. Write follows the same
path — `create_new_file` reported `[success]` for `<project>/.p-ssh/authorized_keys.probe`,
`read_file` read it back, and `apply_patch` deleted it.

Two directories the design treats as unreachable from the sandbox were reached the same way:

```console
list_directory_tree <project>/.p-secrets
  → 3f4c8a….json, 4cb59fc9….json (coffee-cards), 522ef018….json, 9451a767….json, f0537e80….json

read_file <project>/.p-secrets/4cb59fc9….json
  → {"project_path": "/home/chrisng/git/coffee-cards", "secret": "edddc637…"}

JUNIE-RELAY 1 mcp-port 4cb59fc9… edddc637…  →  JUNIE-RELAY 1 mcp-port 64562
```

So finding 4's "`relay-secrets` is not mounted into the sandbox" held for mounting and not for
reading: the disclosed secret was live against the relay for another project's digest. And the
mount approval was forgeable, which is why finding 4's fix did not hold:

```console
create_new_file <project>/.p-appr/f0537e80….json
  with {"project_path": "<project>", "mounts": ["/tmp/junie-sbx-approval-probe"]}
  → [success]                     # in the operator's real approval store

# no record: refused; forged record present, no JUNIE_SBX_APPROVE_MOUNTS: mounted
$ junie-sbx.sh ; echo $?
0
```

`JunieEditService.applyPatch` on the RPC channel is the same escape with no tool allowlist in
front of it, which is what makes finding 7 more than a hygiene issue.

The gate is in `junie-ide-relayd`, because the daemon is the only process that resolves a path
the way the IDE will — the sandbox's `realpath` resolves `~/.ssh` to a path that does not exist
there. `TOOL_PATH_ARGS` names the parameter each tool carries a path in, `escapes` resolves it
with `realpath` against the project the digest resolves to, and a call that lands outside is
refused with `-32602` before the IDE sees it. `apply_patch` is gated on the paths in its body
and `execute_tool` on the path its parsed command names. An in-project symlink whose target is
in the project still works, so `node_modules/.bin` keeps working. `DEFAULT_ALLOW_RPC` closes
the RPC route. `tests/test_relayd.py` covers each shape, asserts the IDE is sent nothing on a
refusal, and fails when a tool whose schema has a path-like parameter is missing from the table.

### 12. The overlay fallback is not verified before use — fixed, not observed

`merge_overlay` treats a failed overlay as harmless: "the merged directory is then empty and
the sandbox keeps its own cache". That is not what a failed overlay leaves. The merged path is
made with `mkdir -p` whether or not a mount follows, so the path the sandbox is handed as its
writable cache is whatever that directory is: the lower tree mounted read-write, or a plain
directory under the state root. The script never checked which, so a merge that did not happen
was mounted and used exactly like one that did.

Both mechanisms that make the merge can fail while reporting success — `fuse-overlayfs` may
exit 0 without mounting, and `sudo mount -t overlay` was never checked at all. That is a real
gap on the evidence of the code, and closing it is what finding 12's fix does.

What this evaluation did **not** establish is that the failure occurs on this host. The
measurement offered for it was taken from inside a sandbox, where `/proc/self/mountinfo`
reports the merged paths as `sbx`-provided virtiofs shares with tag `none`, and where the state
directory is the container's own root rather than the host's. Those numbers cannot show what
the host's overlay did, and on the host the merge is in force: `upper` and `work` are present
and `merged` is a distinct overlay mount. The sandbox-side `stat` and marker tests in an
earlier draft of this finding are withdrawn.

`merge_overlay` now verifies that the merge is really an overlay before the tree is mounted
read-write: `upper` and `work` exist and the merged path is its own mount naming its
`upperdir=`. When it is not, the mount entry carries `:ro` and a warning names `fuse-overlayfs`
as the fix, so a cold write fails visibly instead of reaching the host. The run still finishes.
That is a guard against a state nobody has demonstrated here, which is the right shape for a
check whose failure mode is silent.

## What holds

- **The per-project secret.** Presenting this sandbox's secret: own digest → `mcp-port 64562`;
  `4cb59fc9…` (`coffee-cards`) and `9451a767…` (`auto-mask-shader`) → connection closed; a
  digest naming no project → `gone`. `authorized` resolves both sides with `realpath` and
  compares `resolved == root or resolved.startswith(root + os.sep)`, which is containment
  rather than a prefix match on a string.
- **Fail-closed on exposed records.** A record or the directory holding it that is reachable
  beyond its owner stops the daemon at `EX_CONFIG` 78 with the `chmod` that fixes it, and
  both levels are checked.
- **Constant-time comparison**, on encoded bytes, with a non-ASCII secret skipped and named
  rather than raising.
- **Read-only mounts that resist root**, including a remount attempt.
- **Bounded reads and deadlines**: `MAX_HEAD`, `MAX_MESSAGE`, `PREAMBLE_TIMEOUT`, `IDLE_TIMEOUT`,
  `REAP_TIMEOUT`, `OWED_LIMIT`, and a per-connection guard that keeps one failure from ending
  the daemon.
- **`relay-secrets` is not mounted into the sandbox.** Reading it through a project symlink was
  finding 11, so the mount is one of two things that keep finding 4 from being exploitable; the
  path gate is the other.
- **130 tests pass** under `python3 -B -m unittest discover -s tests`, and the allowlist table
  covers batch, notification, chunked, dispatcher and unparseable shapes, the path gate covers each
  tool shape and escape shape, the RPC allowlist covers forwarded and refused methods, and a
  coverage test fails when a path-bearing tool is missing from the gate's table.

## Priority

1. ~~**Suppress the forwarded SSH agent** for sandboxes that do not push.~~ — done:
   `setup-junie-sbx.sh` turns the daemon-level `ssh.agentForwardingEnabled` off and restarts the
   daemon, since Junie reaches its providers over HTTPS through the proxy and needs no agent.
   **Scope a pushing sandbox to a deploy key** is still open: the setting is all-or-nothing across
   the daemon, and no deploy key for that one sandbox is mounted.
2. ~~**Confine `mounts`** to paths under `$HOME` or the project, refusing `/`, `~` and
   ancestors.~~ — done as an approval instead: a path under the project mounts on its own, one
   outside it only once the operator records it, and the relay secrets are never mountable. An
   entry outside the project is still a wide grant, so approve a directory rather than a tree;
   `tests/test_sbx_mounts.py` drives the cases through a stub `sbx`.
3. ~~**Reconcile the egress policy with `spec.yaml`.**~~ — done: `kit/README.md`, the spec's
   comment on the allowlist and the root README's pointer now say the list is added to sbx's own
   allow-set and the daemon's policy preset rather than replacing either, that `deny-all` is
   where it is the whole surface, and that `sbx policy ls` reports the composition. The `403`
   also names `sbx policy approval ls`, which the README does not.
4. ~~**Narrow the `~/.junie` view** to `AGENTS.md`, `docs/`, `tools/` and this project's
   sessions. Other projects' transcripts are not part of this project's sandbox.~~ — done:
   `init_junie_home` links a session only when `index.jsonl` maps its `sessionId` to this
   project's `projectDir`. The measured view was 89 session directories: 1 this project's, 17
   known to be other projects', and 71 with no index entry. All but this project's are now
   left unlinked, so the failure mode is a session that stops being reachable rather than
   another project's transcript being readable.
5. ~~**Filter the RPC relay** the way the MCP relay is filtered, or state that it is
   unmediated by design and why that survives a plugin that adds a method.~~ — done, and the
   finding understated it: the plugin's dispatch surface is 20 services and 51 methods, not
   one, and the CLI drives most of them. `DEFAULT_ALLOW_RPC` names the methods that read state
   only, and everything else is answered by the daemon with `-32601`. `JunieEditService.applyPatch`
   is the one that mattered: a project write carrying its own paths, so it bypassed both the tool
   allowlist and the path gate, and a project symlink reached the host through it exactly as
   through `read_file`.
6. ~~**Replace the `execute_tool` first-word check** with a check on the dispatched tool and its
   arguments, and settle the `read_*`/`get_*` question `docs/review.md` left open.~~ — done:
   the command is parsed and the dispatched tool's path parameter checked, and the `read_*`
   question is answered in [the finding below](#11-a-project-symlink-reaches-the-host-and-the-check-is-lexical).

## Not verified

- **Whether a direct dial bypasses the MCP allowlist.** The host's MCP port is blackholed
  from the sandbox (`host.docker.internal:64562` → EOF, and the IDE's `41832` likewise), and
  the sandbox-local mirror answered `401 MCP server is running in restricted mode` to every
  header shape tried — `Authorization: Bearer <token>`, the token alone, `X-Api-Key`, and
  `IJ_MCP_AUTH_TOKEN: <token>`. The CLI authenticates in a way not reproduced here, so the
  allowlist's behaviour under a direct dial is unproven in either direction, not shown safe.
- **Whether `sbx` accepts a `mounts` entry of `/`.** `sbx` is absent from this image, so
  finding 4 was measured as the argument `junie-sbx.sh` produces, not as a completed mount. The
  script no longer passes `/`, since mounting it would expose the relay secrets, so the question
  is moot for this repo.
- **Which layer grants each reachable host.** A sample of roughly twenty hosts was probed, and
  some answered for reasons this repo does not name. The layers could not be separated from
  inside the sandbox, where `sbx policy ls` is unavailable.
