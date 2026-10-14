# sbx-junie

My fork(ish) of https://github.com/docker/sbx-kits-contrib/tree/main/junie

Uses your user junie install, allows connection to IDE running on the host. The kit is `kit/`,
described in [`kit/README.md`](kit/README.md); this file is the host side, and how to drive both.

The kit builds no image: the sandbox boots the plain `docker/sandbox-templates:shell`, and the CLI
comes from the host mount.

## Prerequisites

- Junie on the host, at `~/.local/bin/junie` — the sandbox runs that binary rather than installing
  one of its own.
- `sbx`, and `bindfs` for the read-only view of `~/.junie`.
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
sandbox's project — its basename names a per-project `JUNIE_HOME`, and the directory itself is the
writable workspace mount. It pins the provider to `ollama` and the model to
`deepseek-v4.1-flash:cloud`; edit the script to use another.

The host paths it passes:

- `~/.junie/instances` read-only, which is what tells the sandbox which projects the host has open
  ([Connecting to an IDE on the host](#connecting-to-an-ide-on-the-host)),
- the read-only view of `~/.junie`, from which `junie-sbx.sh` links `AGENTS.md`, `docs/`, and
  `tools/` into the per-project `JUNIE_HOME`,
- `~/.local/share/junie` and `~/.local/bin`, where the host's CLI lives.

Auth goes through the sandbox proxy; the credentials the kit declares, and why the allowlist names
no install host, are in [`kit/README.md`](kit/README.md#how-auth-works).

## Connecting to an IDE on the host

Junie can drive a JetBrains IDE you already have open, using it for symbol-aware search, code
inspections, test workflows, and the IDE's own MCP servers. The two relays that make this work, and
the spec's `localhost:27183` entry, are described in
[`kit/README.md`](kit/README.md#relay-architecture); the host steps are below.

Start the daemon before the sandbox:

```console
python3 bin/junie-ide-relayd &
```

It needs no arguments; `--port` and `--instances` override the fixed port and the
`~/.junie/instances` directory it reads. The sandbox-side relay is a no-op until it is up.

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

The IDE's project must be the same project the sandbox has mounted, and the CLI must run from that
project or a child directory — Junie matches the two on path, so a sandbox holding the repository at
a different path selects nothing. Run the sandbox from the project directory itself, as
`junie-sbx.sh` does, rather than copying the project in under another path.

The daemon reads the host's instance files itself, so the mount exists only to tell the sandbox
which projects are open. It holds no state of its own, and killing it costs no more than the sandbox
endpoints failing to connect.

## Customization

Junie's instructions come from the host's Junie home, which is mounted read-only: edit
`~/.junie/AGENTS.md`, and the next sandbox start picks it up.

## Cleanup

Stop the host daemon when you are done with the IDE connection — it is a plain foreground process, so
`Ctrl-C` in its terminal, or `kill` its pid; under the sample unit above,
`systemctl --user disable --now junie-ide-relayd` also keeps it from starting at the next login.
Ending the sandbox leaves it running, and it holds only a listening socket.

Each sandbox leaves a `JUNIE_HOME` under `${XDG_CONFIG_HOME:-~/.config}/junie-sbx/sbx-state/<project>`.
Remove that directory to drop the state for a project you have finished with.
