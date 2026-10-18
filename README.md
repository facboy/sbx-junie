# sbx-junie

My fork(ish) of https://github.com/docker/sbx-kits-contrib/tree/main/junie

Uses your user junie install, allows connection to IDE running on the host. The kit is `kit/`,
described in [`kit/README.md`](kit/README.md); this file is the host side, and how to drive both.

The kit builds your browsers' libraries into its image: the sandbox boots this fork's own
`docker.io/facboy/junie-image`, built from [`kit/Dockerfile`](kit/Dockerfile) on top of
`docker/sandbox-templates:shell-docker` — the standard toolchain plus a Docker engine for the
sandbox's own VM. The CLI comes from the host mount, as do the browsers themselves; the image
carries what they link ([Browser libraries](#browser-libraries)).

## Prerequisites

- Junie on the host, at `~/.local/bin/junie` — the sandbox runs that binary rather than installing
  one of its own.
- `sbx`, and `bindfs` for the read-only view of `~/.junie`.
- `python3` for both relay halves; the daemon reads its config with the stdlib `tomllib`, so 3.11 is
  its floor.
- An API key: `JUNIE_API_KEY`, or a BYOK key from a provider `kit/spec.yaml` declares.

## Setup

Run once, from this directory:

```console
./setup-junie-sbx.sh
```

It exposes `~/.junie` as a read-only bindfs view under
`${XDG_CONFIG_HOME:-~/.config}/junie-sbx/junie_ro/_junie`, so the sandbox can take the host's Junie
home as a workspace without write access. Re-running it is a no-op.

## Run

With the project the sandbox should hold as the current directory:

```console
cd ~/git/some-project
~/git/sbx-junie/junie-sbx.sh
```

The script assumes this repo is cloned at `~/git/sbx-junie`, and takes the current directory as the
sandbox's project — its basename names a per-project directory, and the directory itself is the
writable workspace mount. It pins the provider to `ollama` and the model to
`deepseek-v4.1-flash:cloud`; edit the script to use another.

The host paths it passes:

- `~/.junie/instances` read-only, which is what tells the sandbox which projects the host has open
  ([Connecting to an IDE on the host](#connecting-to-an-ide-on-the-host)),
- the read-only view of `~/.junie`, from which `junie-sbx.sh` links `AGENTS.md`, `docs/`, and
  `tools/` into the Junie home the sandbox writes to, and `junie-init.sh` links the same three
  inside the sandbox's own `~/.junie`, so a tool reads them at `~/.junie/tools`,
- `~/.local/share/junie` and `~/.local/bin`, where the host's CLI lives,
- the host's uv and pnpm trees, read-only, and a writable overlay over each cache
  and the pnpm store ([Your uv and pnpm environments](#your-uv-and-pnpm-environments)), each of
  which a project can leave out with a switch.

Auth goes through the sandbox proxy; the credentials the kit declares, and why the allowlist names
no install host, are in [`kit/README.md`](kit/README.md#how-auth-works).

## Your uv and pnpm environments

The sandbox gets your uv and pnpm trees at their host paths, read-only, so its own `uv` and `pnpm`
resolve the interpreters, tools and store you already have. Because a venv records its interpreter
by absolute path, and pnpm keys its store to a version and a location, these are reached by
environment variable rather than by copying them somewhere else:

- `~/.local/share/uv` read-only, with `UV_PYTHON_INSTALL_DIR` and `UV_TOOL_DIR` pointing into it, so
  `uv venv` reuses your interpreters instead of downloading, and a project's existing `.venv`
  resolves again. `~/.local/share/fnm` rides along read-only and `junie-init.sh` links its node,
  since the pnpm that matches your store needs the node it was built for.
- `~/.cache/uv` and the pnpm store under `~/.local/share/pnpm` are mounted through a per-project
  overlay: your copies are the read-only lower layer, and everything the sandbox writes goes to an
  upper layer under `${XDG_CONFIG_HOME:-~/.config}/junie-sbx/sbx-state/<project>/overlays`, never to
  the host trees. That is what lets `uv` and `pnpm` reuse a cache instead of re-downloading.
  `pnpm`'s store and cache paths reach it through a `~/.npmrc` that `junie-init.sh` writes.
- `~/.cache/ms-playwright` is overlaid the same way, for the same reason: `npx playwright install`
  writes there, so the sandbox needs somewhere to put a browser without touching your cache.
  `PLAYWRIGHT_BROWSERS_PATH` names the merged tree. The libraries those browsers link are the
  image's job, not this one ([Browser libraries](#browser-libraries)).

Overlays are created on the host, since a sandbox cannot mount. They need either `fuse-overlayfs`
or passwordless `sudo`; without one of those, `junie-sbx.sh` says so and the sandbox caches
locally instead.

## Browser libraries

The sandbox runs the browsers from your Playwright cache, but the libraries they link are not yours to
supply: the base image carries almost none of them, and `chrome` exits with
`error while loading shared libraries: libglib-2.0.so.0`. They are installed into the image instead,
by [`kit/Dockerfile`](kit/Dockerfile), which runs Playwright's own `install-deps` for the pinned
release so the package names follow the distro rather than a list kept by hand.

That covers Chromium, Firefox and WebKit, and brings in what they read at run time rather than link: the
DRI drivers headless WebKit renders through, without which it aborts with
`Could not create EGL display: no supported platform available`, and the fonts fontconfig
enumerates, without which text measures as zero-width.

Consequences worth knowing:

- The image is what makes the browsers work, so a sandbox on a bare template has none of this and
  fails with the `libglib-2.0.so.0` error above.
- The dependency set tracks the Playwright release the image was built for (`PLAYWRIGHT_VERSION` in
  the Dockerfile). A host on a newer release works while that release needs nothing extra; one whose
  browsers start failing on a missing soname means the image is behind and wants a rebuild.
- Only WebKit is distro-pinned tightly enough to matter. Playwright picks the 26.04 bundle from
  `1.61` on, and that is the one this base can run; a project pinned below it installs the 24.04
  WebKit, which links `libicu*.so.74` and `libxml2.so.2` against a base shipping ICU 78 and
  `libxml2.so.16`, and ICU embeds its major version in every symbol name, so the older link cannot be
  satisfied. `npx playwright install --force webkit` on the host at `1.61` or later replaces it: the
  revision is unchanged between those bundles, so a plain install sees the directory complete and
  leaves it alone.
- Nothing about the library path can break from inside a running sandbox: there is no mount to go
  stale, which is why the earlier gather-and-register path is gone.

### Switching a tree off

A project that does not want either tree sets two booleans in `.junie/sbx.toml` at the project
root:

```toml
uv = true
pnpm = false
```

Both default to on, so a project with no file behaves as it always did. `pnpm = false` drops the
pnpm tree, its store and cache, and with them the host's node and fnm tree and the Playwright
browsers under `~/.cache/ms-playwright`: the pnpm that matches your store needs the node it was
built on, and the browsers it drives are the rest of that tool chain. `uv = false` drops the uv
tree, its cache and its tools, and leaves pnpm alone. A tree left off is never mounted, so it is
absent from the sandbox rather than present and unused, and its cache and store stay the host's
alone.

`JUNIE_SBX_UV=0` and `JUNIE_SBX_PNPM=0` override the file for one run, taking precedence over it.
A value that is neither `true` nor `false` stops the run and names it: a typo would otherwise read
as on, which is the default.

## Connecting to an IDE on the host

Junie can drive a JetBrains IDE you already have open, using it for symbol-aware search, code
inspections, test workflows, and the IDE's own MCP servers. The two relays that make this work, the
mirror of the IDE's built-in MCP server, and the spec's `localhost:27183` entry are described in:
[`kit/README.md`](kit/README.md#relay-architecture); the host steps are below.

Start the daemon before the sandbox:

```console
python3 bin/junie-ide-relayd &
```

It needs no arguments; `--port` and `--instances` override the fixed port and the
`~/.junie/instances` directory it reads, `--secrets` points at another directory of per-project
secrets, and `--config` points at another allowlist file, which
[MCP tools](#connecting-to-an-ide-on-the-host) below covers. The sandbox-side relay is a no-op until
it is up.

A sandbox reaches only its own project's IDE. `junie-sbx.sh` writes one secret per project under
`${XDG_CONFIG_HOME:-~/.config}/junie-sbx/relay-secrets`, readable only by you, and hands it to the
sandbox; the daemon splices a connection only when the secret matches the project the digest
resolves to. Loopback is shared with every other local process, so without that check any process on
the host — another user, another sandbox — could name another project's digest and reach that
project's IDE. A daemon with no records relays nothing, and says so at startup.

The daemon refuses to start while a record, or the directory holding them, is reachable beyond its
owner — `chmod 600` on the records and `chmod 700` on the directory, which is what `junie-sbx.sh` and
`setup-junie-sbx.sh` write. A secret another local user can read is not a credential, and starting
anyway would keep a reach you believe is denied. Loosening a record while the daemon runs revokes its
access on the next connection, without a restart.

It is a foreground process, so it can be left in a terminal or put under whatever supervisor you
already use. `systemd/user/junie-ide-relayd.service` is a sample user unit for it: copy that file
into `~/.config/systemd/user/`, edit the path in `ExecStart` if your clone is not at
`~/git/sbx-junie`, then

```console
systemctl --user daemon-reload
systemctl --user enable --now junie-ide-relayd
```

A user unit needs no root and starts the daemon at login. For it to start at boot instead, before
any login, allow the user manager to run without one: `sudo loginctl enable-linger "$USER"`. Check
that it is listening with `systemctl --user status junie-ide-relayd` —
`listening on 127.0.0.1:27183` in its log is what the sandbox-side relay needs.

`~/.junie/instances` may be empty on a host that has never opened a project in a JetBrains IDE with
the Junie plugin installed: the directory exists only once the plugin has run. The relay is a no-op
until it does.

Then start the sandbox and check the connection from inside:

```console
> /ide
```

A `Connected` state names the IDE, its project, and the plugin version. `Missing` means the Junie
plugin is not installed in the IDE you have open; `Unsupported` means the IDE is older than
`2026.1`, which is Junie's floor for this integration (see upstream's
[JetBrains IDE integration](https://junie.jetbrains.com/docs/junie-cli-jetbrains-ide-integration.html)).
`AuthRequired`, shown as `Unauthorized`, means the plugin refused the token in the instance file the
relay published — that file named a different IDE from the one the relay routes to. The relay
rebuilds the file from the host's instance files on every pass, so it recovers on its own, without a
sandbox restart.

The IDE's project must be the same project the sandbox has mounted, and the CLI must run from that
project or a child directory — Junie matches the two on path, so a sandbox holding the repository at
a different path selects nothing. Run the sandbox from the project directory itself, as
`junie-sbx.sh` does, rather than copying the project in under another path.

The daemon reads the host's instance files itself, so the mount exists only to tell the sandbox
which projects are open. It holds no state of its own, and killing it costs no more than the sandbox
endpoints failing to connect.

An IDE's own MCP tools ride the same relay: the CLI dials the IDE's built-in MCP port itself, and
the sandbox-side endpoint binds that port so the dial lands on the relay. That server reports as
connected rather than failed in `/mcp`, and the startup MCP setup notice goes away. The sandbox relay
holds a mirror only while the daemon reports the IDE's port, and frees it when the IDE goes away or
names no built-in server, so a port the IDE gave up is not held open.

Those tools reach the host, so the daemon allowlists them rather than passing the IDE's list
through: a tool the allowlist does not name is removed from `tools/list` and refused before the IDE
sees the call, which is what keeps a tool a later IDE build adds from arriving unreviewed. Nothing
that runs project-controlled code on the host is named, and nothing that addresses host state the
sandbox could then reach through it, because the project is shared with the host writable and so is
the payload: `build_project` runs its build tooling, `lint_files` and `reformat_file` its configured
linters and formatters, `execute_run_configuration` a run point the sandbox writes into the project
as an executable script, and `execute_sql_query` a statement against a configured database;
`execute_terminal_command` runs a shell command outright. A Jupyter kernel is the same shape over a
longer reach — it holds live objects, and `configure_python_interpreter` names an interpreter path
the sandbox can write — so `run_notebook_cell`, `execute_code_on_kernel`, `interrupt_notebook` and
`kill_notebook` are withheld too. The notebook *file* tools stay: `create_notebook`,
`edit_notebook`, `read_notebook`, `read_notebook_cell` and `get_notebook_state` touch `.ipynb` files
without executing anything, which is the reach `apply_patch` already has. A build belongs in the
sandbox's own Docker engine, which the kit's base image carries, rather than on the host.

Edit the list in a config file rather than in the unit's `ExecStart`, at
`${XDG_CONFIG_HOME:-~/.config}/junie-sbx/relayd.toml` — the same root `setup-junie-sbx.sh` puts its
read-only view and per-project state under. It is TOML — the stdlib `tomllib` reads it, so the
daemon needs no dependency beyond `python3` 3.11 — and it names the tools as patterns:

```toml
allow_tools = ["apply_patch", "get_*", "read_*"]
```

A file that is absent leaves the built-in list in force. `--config PATH` and
`$JUNIE_IDE_RELAYD_CONFIG` point somewhere else, and `--allow-tool NAME` (an exact name, or a name
ending in `*`) overrides the file. `--no-tool-filter` relays the IDE's list unfiltered. An
`allow_tools` list that is empty is not unfiltered — it names no tool, so every call is refused. A
file that will not parse stops the daemon at startup, and one that will not parse on a reload leaves
the list already in force.

Every entry is a grant to run something on the host, so adding one back widens the boundary the
defaults draw — the tools named above are withheld for that reason, not because they are unused.

`SIGHUP` re-reads the file, so a change does not drop the connections a running sandbox holds:
`systemctl --user reload junie-ide-relayd` under the sample unit, or `kill -HUP` on the daemon's pid.
The list in force is named in the log at startup and at each reload, with the source it came from.

The two halves speak a versioned protocol, and the sandbox publishes nothing against a daemon older
than it needs — a daemon started before one of its preambles closed its connection silently, which
reads as a dead IDE. So restart the daemon after pulling a change to it:

```console
systemctl --user restart junie-ide-relayd
```

The sandbox's watcher picks the work up within a tick, without restarting the sandbox.

## Customization

Junie's instructions come from the host's Junie home, which is mounted read-only: edit
`~/.junie/AGENTS.md` on the host, and the next sandbox start picks it up.

Inside the sandbox, `~/.junie` is the CLI's own directory, not the Junie home: the CLI resolves its
instance files from `user.home/.junie/instances` and never consults `JUNIE_HOME`, so `junie-init.sh`
links `AGENTS.md`, `docs/` and `tools/` beside that `instances/` rather than moving the directory.
That is what makes the read-only view reachable at the paths a tool expects — including
`~/.junie/tools/check_doc_prose.py`.

## Cleanup

Stop the host daemon when you are done with the IDE connection — it is a plain foreground process, so
`Ctrl-C` in its terminal, or `kill` its pid; under the sample unit above,
`systemctl --user disable --now junie-ide-relayd` also keeps it from starting at the next login.
Ending the sandbox leaves it running, and it holds only a listening socket.

Each project gets one directory under `${XDG_CONFIG_HOME:-~/.config}/junie-sbx/sbx-state/<project>`:
`_junie/` is the Junie home the sandbox writes to, and `overlays/` holds the writable uv and pnpm
layers. Remove that directory to drop the state for a project you have finished with.

Delete a project's file under `${XDG_CONFIG_HOME:-~/.config}/junie-sbx/relay-secrets` to revoke that
sandbox's access to the host's IDEs; a later run provisions a fresh secret.
