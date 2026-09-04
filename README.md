# FRP Multi-node Manager

`frpctl` manages one mapping inventory and mirrors it to multiple public FRP servers. Each public node has its own token, SSH tunnel and `frpc` systemd instance, so nodes can coexist and fail independently.

The tool defaults to FRP `0.70.0`. It downloads official release assets and validates them against the release's `frp_sha256_checksums.txt` before installation.

## Install

Python 3.11 or newer is required. Install the manager on the inventory/controller host (`192.168.1.109` for the initial deployment):

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
sudo .venv/bin/frpctl import --apply
```

The default desired-state files are:

```text
/etc/frp-manager/inventory.yaml
/etc/frp-manager/secrets.yaml
```

Both files are atomically written with mode `0600`. Passwords entered by the CLI/TUI are never saved. `secrets.yaml` stores only generated/imported FRP tokens.

When using the default `/etc/frp-manager` location, run every command with `sudo`, for example `sudo frpctl doctor`; the inventory and token files are intentionally not readable by ordinary users. For development, use a user-owned path such as `--inventory ./inventory.yaml` instead.

To use a different location during development, pass `--inventory ./inventory.yaml` to every command.

## Import the existing node

Run this on `192.168.1.109` to preview the current `/etc/frp/frpc.toml`:

```bash
sudo frpctl import
```

After reviewing all mappings, save them:

```bash
sudo frpctl import --apply
sudo frpctl doctor
sudo frpctl node list
sudo frpctl mapping list
```

Import records `203.0.113.123` as a legacy node and does not restart or rewrite its current services.

## Add the clean Ubuntu 24.04 node

The login user and credentials are prompted interactively:

```bash
sudo frpctl node add 198.51.100.20 --user ubuntu --dry-run
sudo frpctl node add 198.51.100.20 --user ubuntu
```

Deployment performs these operations:

1. Validate SSH, sudo, systemd, architecture and port availability.
2. Install and verify `frps 0.70.0` on the public node.
3. Bind its control port to `127.0.0.1:7000`.
4. Generate a node-specific FRP token and restricted Ed25519 tunnel key.
5. Install `frp-tunnel@node-198-51-100-20` and `frpc@node-198-51-100-20` on the client.
6. Copy every mapping from the selected client and verify both services.
7. Save desired state only after deployment succeeds.

Cloud security groups are not changed automatically. Open the desired public mapping ports, but do not expose TCP 7000.

## Manage and synchronize

```bash
frpctl status
frpctl plan node-198-51-100-20
frpctl mapping add metrics 9100 19100
frpctl sync --node node-198-51-100-20 --dry-run
frpctl sync --node node-198-51-100-20
frpctl sync --all
frpctl tui
```

`sync` validates both generated TOML files, replaces them atomically, restarts only the selected node instance, and attempts to restore backups if validation or restart fails.

## Clone a node

`frpctl node clone` deploys a new node carrying an existing node's whole configuration, so growing the fleet does not mean retyping it:

```bash
sudo frpctl node clone node-198-51-100-20 203.0.113.50 --dry-run
sudo frpctl node clone node-198-51-100-20 203.0.113.50
```

Everything is inherited: the FRP version, the control bind port, both vhost ports, every port override, and the SSH user, port, key file, jump host and outbound proxy. Only identity and address change — the new node gets `node-<host>` (or `--node-id`), the address you name, and a freshly allocated tunnel port. Override any inherited field with `--user`, `--ssh-port`, `--key-file`, `--proxy-jump`, `--http-proxy`, or drop the source's proxy with `--no-http-proxy`.

Two things are deliberately **not** copied. The node token and the Ed25519 tunnel key are generated per node id during deployment, which is what lets the twin fail independently of its source. And `legacy_tunnel` is always cleared, since a freshly deployed node uses the managed tunnel layout even when cloned from an imported one.

The client is resolved from the source: if exactly one client serves it, the clone serves that same client and therefore mirrors the same mappings. Pass `--client` when the source serves several.

If the source carries port overrides — which is what `frpdomain apply` leaves behind after moving FRP off port 80 — the clone inherits them and the command says so. The twin then serves that mapping on the relocated port with nothing in front of it until you clone the domains too:

```bash
sudo frpdomain clone node-198-51-100-20 --to node-203-0-113-50
sudo frpdomain dns --node node-203-0-113-50
sudo frpdomain apply --node node-203-0-113-50 --email admin@example.com
```

`frpdomain clone` copies every hostname binding, the certificate contact address and the fallback port onto the target node, and refuses to replace existing bindings unless you pass `--overwrite`. Hostnames are copied verbatim, which is what a second node fronting the same names needs; only desired state changes, so nothing is installed until `apply` runs on the target.

## Terminal UI

`frpctl tui` opens an inventory browser. The Overview tab holds a Nodes table and a Mappings table; every action works on the row highlighted in the focused table, so nothing has to be retyped:

```text
↑ ↓   move the cursor            r   reload the desired state from disk
d     delete the highlighted row s   synchronize the highlighted node
e     edit the highlighted mapping (Enter does the same)
c     clone the highlighted node into the Add node form
p     set the outbound proxy of the highlighted host
q     quit
```

`d` opens a confirmation dialog before writing anything. Deleting a node drops it from the desired state and forgets its token; `frps` on the node and the `frpc@`/`frp-tunnel@` units on the client keep running, so remove them there by hand. Deleting a mapping only changes the desired state — the nodes keep serving it until the next sync.

`s` prompts for the SSH and sudo passwords of that node and its client, then offers `Dry run` or `Sync`. Passwords are used for the single run and never written to disk. Long-running deploys and syncs stream their check results into the log pane at the bottom.

The Add node tab performs the same work as `frpctl node add`, with `Preflight` for a dry run and `Deploy` for the real thing. The Mapping tab adds a mapping or, once `e` has loaded one, saves changes to it; `locations` and any extra keys carried over from an imported `frpc.toml` are preserved across an edit.

`c` on a Nodes row turns that tab into `frpctl node clone`: the SSH user, port and proxy are prefilled from the source, the client is set to the one the source serves, and a banner at the top names the source together with the FRP settings the twin will inherit. Type the new host, fill in the passwords and press `Deploy`. `Reset` clears the form and forgets the source, so the next deploy inherits nothing.

## Build x86_64 artifacts

Nothing in the manager is compiled, but Paramiko depends on native extensions (`cryptography`, `PyNaCl`, `bcrypt`, `cffi`, `PyYAML`), so an x86_64 target needs x86_64 wheels. `scripts/build_x86_64.py` assembles them without compiling anything, which means it produces identical output whether the build host is aarch64, x86_64 or macOS:

```bash
.venv/bin/python scripts/build_x86_64.py --self-extracting
```

That writes to `dist/`:

```text
frp-manager-0.1.0-linux-x86_64/        directory with bin/frpctl and bin/frpdomain
frp-manager-0.1.0-linux-x86_64.tar.gz  the same tree, ready to copy to the node
frp-manager-0.1.0-linux-x86_64.run     single file, unpacks on first use
```

The bundle carries its own relocatable CPython from [python-build-standalone](https://github.com/astral-sh/python-build-standalone), so the target host needs neither Python nor pip nor network access:

```bash
tar -xzf frp-manager-0.1.0-linux-x86_64.tar.gz
sudo ./frp-manager-0.1.0-linux-x86_64/bin/frpctl doctor
```

The single-file variant takes the tool name as its first argument, and unpacks into `${XDG_CACHE_HOME:-~/.cache}/frp-manager` (override with `FRP_MANAGER_HOME`):

```bash
sudo ./frp-manager-0.1.0-linux-x86_64.run frpctl node list
sudo ./frp-manager-0.1.0-linux-x86_64.run frpdomain list
```

Before writing the archive, the script checks the ELF header of the bundled interpreter and of every `.so` it installed, and fails the build if anything is not `EM_X86_64`.

Where `objects.githubusercontent.com` is unreachable, point the CPython download at a release mirror or hand it a tarball you already have:

```bash
.venv/bin/python scripts/build_x86_64.py \
  --release-base-url https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone
.venv/bin/python scripts/build_x86_64.py --cpython-archive ~/Downloads/cpython-3.13.15+20260825-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz
```

If the target already has Python 3.11 or newer, `--mode wheelhouse` skips the interpreter and emits just the x86_64 wheels:

```bash
.venv/bin/python scripts/build_x86_64.py --mode wheelhouse
# on the x86_64 host, next to the copied wheelhouse and source tree:
pip install --no-index --find-links wheelhouse-x86_64 .
```

A single-file native executable in the PyInstaller sense is not produced here: PyInstaller and Nuitka cannot cross-compile, so building one requires an x86_64 machine (or `docker run --platform linux/amd64`). The `.run` file is the closest equivalent that a non-x86_64 host can build.

## Outbound proxy per host

Deployment downloads `frps`/`frpc` from GitHub releases on both the node and the client. Each host carries its own proxy setting, independently of every other host: different URLs, different schemes, or none at all.

```bash
# the client reaches GitHub through a local SOCKS proxy
sudo frpctl proxy set socks5h://127.0.0.1:1080 --client client-109

# one node uses a proxy on its own LAN
sudo frpctl proxy set http://10.0.0.8:3128 --node node-123

# another node has direct access, so it needs nothing at all
sudo frpctl proxy list
sudo frpctl proxy clear --node node-198-51-100-20
```

No proxy is the default: a host whose `ssh.http_proxy` is unset gets no `export` prologue and connects directly. Setting one side never touches the other, and a node's setting is per node — three nodes can have three different proxies, or two proxies and one direct route.

`frpctl node add` accepts `--http-proxy` for the node it creates, and the TUI sets it with `p` on the highlighted row: a Nodes row targets that node, a Mappings row targets that row's client.

The URL is resolved **on the target machine**, so `http://127.0.0.1:7890` means a proxy listening on that host — not one reachable from the controller. Accepted schemes are `http`, `https`, `socks4`, `socks4a`, `socks5` and `socks5h`.

The value is stored as `ssh.http_proxy` in the inventory and exported by `SSHExecutor.run` as `http_proxy`, `https_proxy`, `all_proxy`, `no_proxy` and their uppercase spellings, in front of every remote command:

```sh
sudo -S -p '' sh -c 'export http_proxy=…; …; curl -fsSL --retry 3 https://github.com/…'
```

Because the exports live inside the shell that `sudo` starts, this needs **no** `env_keep` entry in `/etc/sudoers.d`, no `/etc/environment` edit and no shell profile on the remote host. `apt-get` picks the same variables up, so the `curl`/`tar` bootstrap is proxied too. `no_proxy` covers `localhost,127.0.0.1,::1` so the loopback FRP control port is never sent through the proxy.

One command deliberately opts out: the ACME challenge probe in `frpdomain apply` runs with `proxy=False`, because it has to travel the public DNS/CDN path back to the node's own Nginx and a proxy would validate the wrong route.

Preflight now proves reachability before anything is installed. `frpctl node add --dry-run` (or `Preflight` in the TUI) fetches `frp_sha256_checksums.txt` from both hosts with a 25 second ceiling and reports `node-frp-download` / `client-frp-download`, quoting curl's own error and the proxy in use. Previously a blocked route surfaced only as a three-minute stall inside `install_frp`, where curl is allowed three retries under a 180 second timeout.

## Per-node Domains and Automatic HTTPS

`frpdomain` installs Nginx and Certbot on a selected public node. DNS records remain manual, while certificate issuance and renewal are automatic.

If FRP currently owns TCP port 80, the tool moves that mapping to an unused node-specific internal port (normally `10080`) before starting Nginx. Nginx then proxies direct IP access back to that port, so `http://198.51.100.20` continues to work. Other FRP nodes keep their original port mappings.

Create the desired bindings:

```bash
sudo .venv/bin/frpdomain add example.com --node node-198-51-100-20 --port 80
sudo .venv/bin/frpdomain add api.example.com --node node-198-51-100-20 --port 13000
```

Show the DNS records that must be added manually:

```bash
sudo .venv/bin/frpdomain dns --node node-198-51-100-20
```

Add each displayed A record at the DNS provider, wait for propagation, then preview and apply:

```bash
sudo .venv/bin/frpdomain plan --node node-198-51-100-20
sudo .venv/bin/frpdomain apply --node node-198-51-100-20 --email admin@example.com
```

If the domain is behind Alibaba ESA or another CDN, DNS will resolve to edge IPs rather than `198.51.100.20`. In that case use the edge mode; it still requires the edge to forward `/.well-known/acme-challenge/` to the origin:

```bash
sudo .venv/bin/frpdomain apply \
  --node node-198-51-100-20 \
  --email admin@example.com \
  --edge-proxy
```

For every bound hostname, configure ESA to use `198.51.100.20` with HTTP origin port `80` and preserve the original Host header. Before invoking Certbot, the tool writes a temporary ACME challenge file and fetches it through the public ESA hostname. Certificate issuance proceeds only if that round trip succeeds.

Use `--skip-dns-check` only when DNS validation is handled externally; the ACME round-trip check still runs. If ESA blocks the challenge path, add a bypass/cache rule for `/.well-known/acme-challenge/*` or use an ESA-managed/origin certificate workflow. This tool does not store Alibaba API credentials.

The resulting behavior is:

```text
http://198.51.100.20       -> existing web mapping
http://example.com             -> redirects to HTTPS
https://example.com            -> existing web mapping
http://api.example.com         -> redirects to HTTPS
https://api.example.com        -> FRP port 13000
198.51.100.20:13000        -> remains directly accessible
```

Certificates are stored under `/etc/letsencrypt/live/frpdomain-<node-id>/`. The tool enables `certbot.timer` and installs a renewal deploy hook that reloads Nginx.

### Domain terminal UI

`frpdomain tui` browses the bindings the same way `frpctl tui` browses nodes and mappings:

```text
↑ ↓   move the cursor            r   reload the desired state from disk
d     delete the highlighted binding
e     edit the highlighted binding (Enter does the same)
c     copy every binding of its node onto another node
g     render the final Nginx configuration for its node
a     apply domains and HTTPS to its node
q     quit
```

Four tabs: **Bindings** is the table every action works on, **Binding** is the add/edit form, **DNS** lists the A records to create manually at the provider, and **Nginx plan** holds whatever `g` last rendered. The DNS table is read-only, so `d`, `e`, `g` and `a` always act on the highlighted row of the Bindings table no matter which one has focus.

Saving a binding whose hostname you changed while editing renames it rather than leaving the old row behind, and hostnames are lowercased with any trailing dot stripped. A binding for a node that `frpctl` does not manage is refused at save time instead of failing much later inside `apply`.

`g` previews on a deep copy of the inventory, so the port-80 relocation it shows is not written to disk; it reports the port FRP would move to. `a` opens one form with the certificate email (prefilled from the stored value), the SSH and sudo passwords for the node and the client, and the `--skip-dns-check` / `--edge-proxy` switches, then streams progress into the log pane. Nothing is applied until that form is submitted, and the passwords are used for the single run.

The client is resolved from the frpctl inventory: if exactly one client serves the node it is used automatically, otherwise the Client ID field on the Binding tab breaks the tie.

`c` runs `frpdomain clone` from the highlighted row: it asks which node should receive the copy, listing the managed node ids, and warns before overwriting bindings the target already has. Only desired state changes, so follow it with the DNS records and `a` on the target.

## Security notes

- Run the manager under an account allowed to read the inventory and secrets files.
- Prefer an unprivileged SSH deployment user with sudo over root password login.
- Tunnel keys are restricted with `permitopen=127.0.0.1:7000` and cannot allocate unrelated forwarding destinations.
- Existing `/etc/frp/frpc.toml` and `/etc/frp/frps.toml` files currently have mode `0644`; change them to `0600` after the legacy node is safely migrated.
- Review the host key collected during bootstrap before relying on a new node in a hostile network.

## Tests

```bash
.venv/bin/pytest
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
```

The TUI tests drive both apps headlessly through Textual's `Pilot`, so deletion, editing, the proxy dialog and the Nginx preview are covered without a terminal or a remote host.

## Continuous integration

Two workflows run on GitHub Actions.

`.github/workflows/ci.yml` lints once with the pinned ruff from the `lint` extra, then runs the test suite on CPython 3.11, 3.12, 3.13 and 3.14.

`.github/workflows/build.yml` runs `scripts/build_x86_64.py` on every push to `main`, on `v*` tags, on pull requests that touch the build script, and on demand. Because the runner is itself x86_64, it does what a cross-build host cannot: **execute** the artifact it just assembled.

- the bundled interpreter reports `platform.machine() == "x86_64"`
- `paramiko`, `cryptography`, `PyNaCl`, `bcrypt`, `PyYAML`, `textual` and `typer` all import, which is the only real proof that the cross-downloaded native wheels work
- `bin/frpctl` and `bin/frpdomain` run `--help`, `doctor` and `list`
- the `.run` unpacks, dispatches by argument, reuses its cache on a second call, dispatches by symlink name, and rejects an unknown tool name

The tarball, the `.run` and a `SHA256SUMS` file are uploaded as a build artifact, and a `v*` tag additionally publishes them as a GitHub release:

```bash
git tag v0.1.0 && git push origin v0.1.0
```
