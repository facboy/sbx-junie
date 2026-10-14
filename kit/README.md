# junie

A standalone sandbox kit for [Junie](https://junie.jetbrains.com/), the AI coding agent by JetBrains. The
kit runs on a **pre-baked sandbox image** — Junie is installed from its stable channel at image-build time, not at
sandbox creation, so a new sandbox starts in seconds instead of waiting on the vendor install script. The kit itself
wires Junie's API auth through the sandbox proxy and runs `junie` as the entrypoint.

## Prerequisites

- A [Junie](https://junie.jetbrains.com/) account and API key (or an API key from a supported LLM provider for BYOK).
- Environment variables like `JUNIE_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `XAI_API_KEY`, or
  `OPENROUTER_API_KEY` exported on your host.

## Setup

Junie is LLM-agnostic. You can provide your `JUNIE_API_KEY` or use Bring Your Own Key (BYOK) from providers like
Anthropic, OpenAI, Google, xAI, or OpenRouter.

To use Junie with your **JetBrains AI subscription** in a headless environment (like Docker Sandbox), it is recommended
to use a `JUNIE_API_KEY`. You can generate one at [junie.jetbrains.com/cli](https://junie.jetbrains.com/cli).

The kit is configured to use the sandbox proxy for secure authentication. Secrets stay on the host and are injected by
the proxy on outbound requests.

To use Junie with its primary API:

1. Export `JUNIE_API_KEY` on your host.
2. Run the sandbox.

## Usage

Run the kit. Pass the kit's name (`junie`) as the agent argument. The primary
form is its published OCI artifact on Docker Hub:

```console
sbx run --kit "docker.io/sbx/junie-kit:latest" junie
```

Or from a git URL targeting this repo:

```console
sbx run --kit "git+https://github.com/docker/sbx-kits-contrib.git#dir=junie" junie
```

Or with a local clone of this repo:

```console
sbx run --kit ./junie/ junie
```

Attaching drops you straight into Junie; sandbox creation installs nothing — Junie ships inside the image. The image
itself still has to be pulled the first time, if it isn't cached locally.

## Connecting to an IDE on the host

Junie can drive a JetBrains IDE you already have open, using it for symbol-aware search, code
inspections, test workflows, and the IDE's own MCP servers. Junie finds that IDE by reading
`~/.junie/instances/*.json` — a file the IDE's Junie plugin writes, holding the project it has
open and the loopback port it listens on — and then connecting to `127.0.0.1:<port>`.

That directory does not follow `JUNIE_HOME`: the CLI resolves it from the `junie.instances.dir`
system property if set, and otherwise from `user.home` (a JVM property, not `$HOME`), never from
Junie Home. A sandbox given a custom `JUNIE_HOME` still reads `/home/agent/.junie/instances`, which
is why `junie-ide-relay.sh` publishes there and takes `JUNIE_IDE_INSTANCES_DIR` for the property's
override. Set that variable — don't move `JUNIE_HOME` — to relocate the files.

Inside a sandbox that loopback is the sandbox's own, where no IDE runs, so the kit bridges the two
with a pair of relays, one on each side of the boundary:

- **On the host**, `bin/junie-ide-relayd` — at the root of this repo, since nothing in it ships
  inside the kit — listens on one fixed loopback port (`27183`) and splices each incoming connection
  to the IDE that the connection names.
- **In the sandbox**, `junie-ide-relay.sh` runs at every sandbox start. It opens a loopback
  endpoint for the project the sandbox holds, forwards it to that one host port, and writes an
  instance file naming the endpoint's port and pid — Junie only accepts an endpoint whose pid is a
  live process in the sandbox. A second copy runs as a watcher and repeats that pass, so an IDE you
  open on the host *after* the sandbox started appears without restarting anything.

The two are joined by a one-line preamble: the sandbox endpoint opens each connection with the
project's digest and the host daemon answers by resolving that digest against the host's instance
files. The IDE's own ephemeral port is therefore never dialled from the sandbox and never appears in
the allowlist — the sandbox only ever dials the daemon's fixed port. That is what makes
`localhost:27183` in `permissions.network.allow` a real narrowing rather than an opening onto every
host port, and it means an IDE that restarts on a new port keeps working: the daemon re-resolves the
port on every connection.

Only the project the sandbox holds is published, because an IDE can have several projects open and
writes an instance file for each. Junie selects an IDE by project path, so an instance file for any
other project would offer it an IDE with a different project open.

Start the daemon before the sandbox:

```console
python3 bin/junie-ide-relayd &
```

It needs no arguments; `--port` and `--instances` override the fixed port and the
`~/.junie/instances` directory it reads. The sandbox-side relay is a no-op until it is up.

It is a foreground process, so it can be left in a terminal or put under whatever supervisor you
already use. This repo's `systemd/user/junie-ide-relayd.service` is a sample user unit for it:
copy that file into `~/.config/systemd/user/`, edit the path in `ExecStart` if your clone is not at
`~/git/sbx-junie`, then

```console
systemctl --user daemon-reload
systemctl --user enable --now junie-ide-relayd
```

A user unit needs no root and starts the daemon at login. For it to start at boot instead, before
any login, allow the user manager to run without one: `sudo loginctl enable-linger "$USER"`. Check
that it is listening with `systemctl --user status junie-ide-relayd` —
`listening on 127.0.0.1:27183` in its log is what the sandbox-side relay needs.

Then give the instance files as an extra read-only workspace, replacing `~` with your own home
directory:

```console
sbx run --kit "docker.io/sbx/junie-kit:latest" junie . ~/.junie/instances:ro
```

`~/.junie/instances` may be empty on a host that has never opened a project in a JetBrains IDE with
the Junie plugin installed: the directory exists only once the plugin has run. The relay is a no-op
until it does.

Then check the connection from inside:

```console
> /ide
```

A `Connected` state names the IDE, its project, and the plugin version. `Missing` means the Junie
plugin is not installed in the IDE you have open; `Unsupported` means the IDE is older than
`2026.1`, which is Junie's floor for this integration (see upstream's
[JetBrains IDE integration](https://junie.jetbrains.com/docs/junie-cli-jetbrains-ide-integration.html)).

The IDE's project must be the same project the sandbox has mounted, and the CLI must run from that
project or a child directory — Junie matches the two on path, so a sandbox holding the repository at
a different path selects nothing. Mount the project at its own path, as above, rather than copying
it in.

The daemon reads the host's instance files itself, so the mount exists only to tell the sandbox
which projects are open. It holds no state of its own, and killing it costs no more than the sandbox
endpoints failing to connect.

## How auth works

The kit's `credentials` list declares an `apiKey` entry per provider, for `junie.jetbrains.com`, `api.anthropic.com`,
`api.openai.com`, `generativelanguage.googleapis.com`, `api.x.ai`, and `openrouter.ai`. This tells the proxy to inject
the correct authentication headers (e.g., `Authorization: Bearer %s`, `x-api-key: %s`, or `x-goog-api-key: %s`) on
outbound requests.

Each credential sets `apiKey.proxyManaged: true` to ensure it's handled securely by the proxy.

`permissions.network.allow` no longer lists `github.com`, `raw.githubusercontent.com`, or
`release-assets.githubusercontent.com`: those were only ever needed for the vendor installer's own
version-resolution feed and the release zip it downloads, both now resolved at image-build time (see
[Base image](#base-image)), and the image also sets `JUNIE_SKIP_UPDATE_CHECK=1` so the installed binary never checks
for a newer build at runtime either. `junie.jetbrains.com` stays in the allowlist regardless, because it is also the
`junie` credential's inject domain above.

Junie's own multi-channel switching (`junie --eap`, `--nightly`, `--experimental`) is outside this kit's supported
surface: it fetches and installs another channel's build on demand, and that build's hosts are not part of this
allowlist.

`localhost:27183` is in the allowlist for [the host IDE connection](#connecting-to-an-ide-on-the-host), not
for auth: the proxy classifies a dial to `host.docker.internal:<port>` as `localhost:<port>`, so that entry is
what lets the sandbox-side relay reach `junie-ide-relayd` on the host. It names the daemon's fixed port, not the
IDE's.

### Anthropic: API key only, no Claude subscription

Junie's bring-your-own-key mode takes provider **API keys** — the CLI's
own flag is `--anthropic-api-key sk-...` — and has no Claude subscription
(OAuth) login. So the kit declares only an `apiKey` credential for
`anthropic`, and a host whose only Anthropic credential is a subscription
login has nothing usable here: the API-key sentinel would reach Anthropic
unswapped and every model call would 401. Bind an API key instead
(`echo "$ANTHROPIC_API_KEY" | sbx secret set anthropic`), use a
`JUNIE_API_KEY` from junie.jetbrains.com, or pick one of the other
providers the kit declares.

Do not authenticate from inside the sandbox: a credential written into
the container defeats `proxyManaged: true`, since from there it is
readable by the agent and by anything the agent runs. Keep credentials
host-side.

## Base image

Unlike most kits here — which are `kind: mixin` or `kind: agent` and layer
onto an existing `docker/sandbox-templates` image — a `kind: sandbox` kit *is*
the whole environment, so it names the image the sandbox boots from. This kit
builds and publishes its own, from the `Dockerfile` in this directory:

```
docker.io/sbx/junie-image
└── FROM docker/sandbox-templates:shell
    └── junie (upstream's own install.sh, stable channel)
        ENV JUNIE_SKIP_UPDATE_CHECK=1
```

`JUNIE_SKIP_UPDATE_CHECK=1` disables the installed binary's own runtime
auto-update check — without it, Junie's shim reaches out on its own schedule
to look for a newer build of the channel it is on, independent of anything
this kit's egress policy allows. Setting it is what makes the trimmed
allowlist above a closed set rather than an approximation of one.

The `-image` suffix distinguishes the base image from the kit itself: the kit
is published separately as an OCI artifact at `docker.io/sbx/junie-kit` (see
[Usage](#usage) above).

### Building and publishing

How the image is named, tagged, verified and pushed is the same for every kit
in this repo that builds its own image — see
**[PUBLISHING.md](../PUBLISHING.md)** for the pipeline. There is no
kit-specific build script or workflow; CI builds and publishes this image the
same way it does for `hermes-agent`/`pi`/`openclaw`/`kiro`/`copilot`.

Junie's stable channel publishes a new build every few days, so this image
rolls: the `Dockerfile`'s `ADD` against upstream's own version-resolution feed
forces a fresh install whenever the channel moves, and the pipeline's nightly
scheduled rebuild picks one up within a day either way. There is no supported
way to pin a specific build in this image — `install.sh`'s `JUNIE_VERSION`
override exists, but is not exposed as a build arg here (see the Dockerfile).

### Building locally

```console
docker build -t docker.io/sbx/junie-image:latest junie
./scripts/test-kit.sh junie
```

`scripts/test-kit.sh` builds the kit's own image before running the suite
(`SBX_KIT_SKIP_IMAGE_BUILD=1` to skip and reuse what's already built). Until
the image is first published — pull requests build it but never push it — the
TCK's `container` subtest can only pull it locally, so build before you test.

## Customization

Junie's instructions can be customized by editing `.junie/AGENTS.md` or `AGENTS.md`.
Junie automatically detects these files and uses them to guide its behavior.

## Cleanup

Stop the host daemon when you are done with the IDE connection — it is a plain foreground process, so
`Ctrl-C` in its terminal, or `kill` its pid; under the sample unit above,
`systemctl --user disable --now junie-ide-relayd` also keeps it from starting at the next login.
Ending the sandbox leaves it running, and it holds only a listening socket.

Inside the sandbox, `junie-ide-relay.sh` leaves the endpoints and the instance files it wrote there
and in `/home/agent/.junie/instances`. Both go with the sandbox; to undo them in a sandbox you are
keeping, remove the `ide-relay/` directory under Junie Home and the `instances/*.json` files whose
names match the digests listed there.
