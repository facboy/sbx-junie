# junie

A standalone sandbox kit for [Junie](https://junie.jetbrains.com/), the AI coding agent by JetBrains.
This fork runs Junie from the host's own install rather than installing one into the sandbox, so the
base image is left plain and the kit takes on the wiring: the spec's `permissions`, `credentials`,
and `environment`, and the sandbox half of the IDE relay. The CLI itself comes from the host mount,
which `junie-init.sh` links into the agent's home at startup; the mount is the root README's job.

## Prerequisites

- A [Junie](https://junie.jetbrains.com/) account and API key (or an API key from a supported LLM provider for BYOK).
- Environment variables like `JUNIE_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `XAI_API_KEY`, or
  `OPENROUTER_API_KEY` exported on your host.

## Choosing a key

Junie is LLM-agnostic. You can provide your `JUNIE_API_KEY` or use Bring Your Own Key (BYOK) from providers like
Anthropic, OpenAI, Google, xAI, or OpenRouter.

To use Junie with your **JetBrains AI subscription** in a headless environment (like Docker Sandbox), it is recommended
to use a `JUNIE_API_KEY`. You can generate one at [junie.jetbrains.com/cli](https://junie.jetbrains.com/cli).

The kit is configured to use the sandbox proxy for secure authentication. Secrets stay on the host and are injected by
the proxy on outbound requests.

## Usage

This fork is run through the root scripts — `setup-junie-sbx.sh` once, then `junie-sbx.sh` per
project; see the [root README](../README.md#run). The kit directory is what `junie-sbx.sh` passes to
`sbx`.

Upstream's kit is the other way in: `docker.io/sbx/junie-kit`, or
`git+https://github.com/docker/sbx-kits-contrib.git#dir=junie`. Both install Junie into their own
image, and so lack the host-install model and the IDE relay below.

## Relay architecture

Junie reaches an IDE through `~/.junie/instances/*.json` — a file the IDE's Junie plugin writes,
holding the project it has open and the loopback port it listens on. The CLI then dials
`127.0.0.1:<port>`, which inside a sandbox is the sandbox's own loopback where no IDE runs, so the
kit bridges the two with a pair of relays.

That directory does not follow `JUNIE_HOME`: the CLI resolves it from the `junie.instances.dir`
system property if set, and otherwise from `user.home` (a JVM property, not `$HOME`), never from
Junie Home. A sandbox given a custom `JUNIE_HOME` still reads `/home/agent/.junie/instances`, which
is why `junie-ide-relay.sh` publishes there and takes `JUNIE_IDE_INSTANCES_DIR` for the property's
override. Set that variable — don't move `JUNIE_HOME` — to relocate the files.

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

### The IDE's built-in MCP server

The plugin also serves the IDE's own MCP tools, on a port of its choosing, and hands that port to the
CLI over the RPC above. The CLI then dials `http://localhost:<port>/stream` itself — its own
loopback, where nothing listens — so that server reports as failed and Junie raises an MCP setup
notice. The same relay carries it:

- The daemon resolves the port by asking the plugin, over the IDE's RPC, the same
  `JunieMcpService.getBuiltInMcpServerCredentials` call the CLI makes. `JUNIE-RELAY 1 mcp <digest>`
  splices to the server; `JUNIE-RELAY 1 mcp-port <digest>` answers with the port, `none` when the
  plugin names no built-in server, `gone` when the IDE behind the digest is not running, or
  `unknown` when the plugin did not answer.
- `junie-ide-relay.sh` asks for that port on every pass and starts an endpoint bound to it, marked
  `--mcp` so its preamble names the server rather than the RPC. The endpoint owns the port outright,
  on `127.0.0.1` and `::1` alike: `localhost` resolves to the IPv6 loopback first, and a listener on
  one family alone leaves every connection waiting on a refused attempt on the other.
- The IDE picks a new port when it restarts, so a pass that finds a different one kills the endpoint
  holding the old port before binding the new. A pass answering `none` or `gone` frees the mirror
  for the same reason — the port is the IDE's, and nothing will use it again — while `unknown` keeps
  it, since a momentary RPC failure answers that way and dropping a working mirror for one would
  drop the IDE MCP connection until the next pass rebuilt it. No allowlist entry is involved: the
  CLI's dial is to its own loopback, never through the proxy, so this is a listener to provide
  rather than a host to permit.
- The two halves share a protocol version, and the relay script skips the mirror while the daemon
  answers a lower one or none at all. A daemon started before a preamble was added closes its
  connection silently, which reads as a dead IDE, so the `health` reply names the version and the
  script reports the mismatch once rather than leaving it to look like an IDE that is down. Only the
  mirror is skipped: the RPC relay works against any daemon, so an older one costs the IDE's MCP
  server and nothing else. **Restart the daemon after pulling a change to it**, since a running
  daemon keeps the preambles it started with; the sandbox's watcher then picks the work up within a
  tick, without restarting the sandbox.

Starting the daemon, running the sandbox, and checking `/ide` are in the
[root README](../README.md#connecting-to-an-ide-on-the-host).

## How auth works

The kit's `credentials` list declares an `apiKey` entry per provider, for `junie.jetbrains.com`, `api.anthropic.com`,
`api.openai.com`, `generativelanguage.googleapis.com`, `api.x.ai`, and `openrouter.ai`. This tells the proxy to inject
the correct authentication headers (e.g., `Authorization: Bearer %s`, `x-api-key: %s`, or `x-goog-api-key: %s`) on
outbound requests.

Each credential sets `apiKey.proxyManaged: true` to ensure it's handled securely by the proxy.

`permissions.network.allow` lists no install hosts at all — `github.com`,
`raw.githubusercontent.com`, and `release-assets.githubusercontent.com` are absent. Nothing installs
Junie in the sandbox: the CLI is the host's own, and the spec sets `JUNIE_SKIP_UPDATE_CHECK=1`, which
stops that binary from fetching and staging a newer build at runtime. `junie.jetbrains.com` stays in
the allowlist regardless, because it is also the `junie` credential's inject domain above.

Junie's own multi-channel switching (`junie --eap`, `--nightly`, `--experimental`) is outside this kit's supported
surface: it fetches and installs another channel's build on demand, and that build's hosts are not part of this
allowlist.

`localhost:27183` is in the allowlist for [the relay](#relay-architecture), not
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

A `kind: sandbox` kit *is* the whole environment, so it names the image the sandbox boots from. This
fork names `docker/sandbox-templates:shell-docker`: the standard sandbox toolchain plus a Docker
engine, and no Junie in it. The engine is the sandbox's own, inside its VM — a sandbox has no path to
the host's daemon — and the template already carries `com.docker.sandboxes.start-docker=true`, which
is what asks the runtime to start it. The CLI comes from the host instead — the root scripts mount
`~/.local/bin` and `~/.local/share/junie` read-only, and `junie-init.sh` symlinks them into
`/home/agent` at startup so the `junie` entrypoint resolves.

That is also why nothing here builds or publishes an image, and why the kit's egress allowlist can
name no install host: the vendor install script never runs, in the sandbox or at image build. The
engine adds no host to the allowlist either, since nothing here installs through it at create time.

`JUNIE_SKIP_UPDATE_CHECK=1`, set by the spec's `environment`, seals the installed binary's own
runtime auto-update check. Without it the shim polls for and stages a newer build of the channel it
is on, independent of anything this kit's egress policy allows.

## Cleanup

Inside the sandbox, `junie-ide-relay.sh` leaves the endpoints and the instance files it wrote there
and in `/home/agent/.junie/instances`. Both go with the sandbox; to undo them in a sandbox you are
keeping, remove the `ide-relay/` directory under Junie Home and the `instances/*.json` files whose
names match the digests listed there.
