from __future__ import annotations

import secrets
import shlex
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from .errors import DeploymentError, ValidationError
from .frp import effective_remote_port, render_frpc, render_frps
from .models import Client, Inventory, Node
from .ssh import SSHExecutor

FRP_RELEASES = "https://github.com/fatedier/frp/releases/download"
ARCH_MAP = {
    "amd64": "linux_amd64",
    "arm64": "linux_arm64",
    "aarch64": "linux_arm64",
    "armhf": "linux_arm_hf",
    "linux_amd64": "linux_amd64",
    "linux_arm64": "linux_arm64",
    "linux_arm_hf": "linux_arm_hf",
}


def release_base(version: str) -> str:
    return f"{FRP_RELEASES}/v{version}"


def tunnel_proxy_command(ssh_proxy: str | None) -> str:
    """Return the ssh ``ProxyCommand`` value the client tunnel dials through.

    The ``frp-tunnel@`` unit runs on the client and connects to the node's SSH
    port. When the node is only reachable through a SOCKS proxy that listens on
    the client itself (e.g. ``socks5h://127.0.0.1:7891``), OpenBSD netcat runs
    the SOCKS handshake: ``-X 5`` selects SOCKS5, ``-X 4`` SOCKS4. ``%h``/``%p``
    are expanded by ssh, not systemd. ``none`` disables proxying so ssh dials
    the host directly.

    ``nc`` only authenticates HTTP CONNECT proxies, so a username is forwarded
    best-effort and a password in the URL cannot be carried.
    """
    if not ssh_proxy:
        return "none"
    parsed = urlparse(ssh_proxy)
    version = "4" if parsed.scheme in ("socks4", "socks4a") else "5"
    endpoint = parsed.hostname or ""
    if parsed.port:
        endpoint = f"{endpoint}:{parsed.port}"
    auth = f" -P {shlex.quote(parsed.username)}" if parsed.username else ""
    return f"nc -X {version} -x {endpoint}{auth} %h %p"


def tunnel_key_paths(node: Node) -> tuple[str, str]:
    """Return the ``(key, known_hosts)`` paths for a node's client tunnel."""
    return (
        f"/etc/frp-manager/keys/{node.id}_ed25519",
        f"/etc/frp-manager/keys/{node.id}_known_hosts",
    )


def tunnel_env_file(node: Node, key: str, known_hosts: str) -> str:
    """Render the ``EnvironmentFile`` the ``frp-tunnel@`` unit reads.

    ``PROXY_OPT`` is always written so the shared unit's ``-o ${PROXY_OPT}``
    resolves to a real argument (``ProxyCommand=none`` when no SOCKS proxy is
    set); an absent value would expand to an empty ``-o`` and break ssh.
    """
    return "\n".join(
        [
            f"LOCAL_PORT={node.tunnel_port}",
            f"REMOTE_PORT={node.frps_bind_port}",
            f"KEY_FILE={key}",
            f"KNOWN_HOSTS={known_hosts}",
            f"SSH_USER={node.ssh.user}",
            f"SSH_HOST={node.ssh.host}",
            f"SSH_PORT={node.ssh.port}",
            f"PROXY_OPT=ProxyCommand={tunnel_proxy_command(node.ssh.ssh_proxy)}",
            "",
        ]
    )


FRPS_UNIT = """[Unit]
Description=FRP server managed by frpctl
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/frps -c /etc/frp/frps.toml
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
"""

FRPC_UNIT = """[Unit]
Description=FRP client for %i managed by frpctl
After=network-online.target frp-tunnel@%i.service
Wants=network-online.target frp-tunnel@%i.service

[Service]
Type=simple
ExecStart=/usr/local/bin/frpc -c /etc/frp/clients/%i.toml
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
"""

TUNNEL_UNIT = """[Unit]
Description=SSH tunnel for FRP node %i
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
EnvironmentFile=/etc/frp/tunnels/%i.env
ExecStart=/usr/bin/ssh -N -L ${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT} -i ${KEY_FILE} -p ${SSH_PORT} -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o StrictHostKeyChecking=yes -o UserKnownHostsFile=${KNOWN_HOSTS} -o ${PROXY_OPT} ${SSH_USER}@${SSH_HOST}
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
"""


@dataclass(slots=True)
class Check:
    name: str
    ok: bool
    detail: str


class Deployer:
    def __init__(self, inventory: Inventory, secrets_data: dict | None = None):
        self.inventory = inventory
        self.secrets = secrets_data or {"nodes": {}}

    def token_for(self, node_id: str) -> str:
        nodes = self.secrets.setdefault("nodes", {})
        if node_id not in nodes:
            nodes[node_id] = {"token": secrets.token_urlsafe(32)}
        return nodes[node_id]["token"]

    def checks(
        self,
        node: Node,
        client: Client,
        remote: SSHExecutor,
        *,
        allow_frps: bool = False,
    ) -> list[Check]:
        checks: list[Check] = []
        commands = [
            ("uname -s", "OS"),
            ("command -v systemctl", "systemd"),
            ("command -v ss", "ss"),
        ]
        for command, label in commands:
            result = remote.run(command, check=False)
            checks.append(
                Check(label, result.ok, (result.stdout or result.stderr).strip())
            )
        for command, label in [
            ("command -v curl", "curl"),
            ("command -v tar", "tar"),
            ("command -v sha256sum", "sha256sum"),
        ]:
            result = remote.run(command, check=False)
            checks.append(
                Check(
                    label,
                    True,
                    result.stdout.strip()
                    if result.ok
                    else "missing; will install during deployment",
                )
            )
        ports = [str(node.frps_bind_port)] + [
            str(effective_remote_port(node, x))
            for x in client.mappings
            if effective_remote_port(node, x)
        ]
        protocols = {x.protocol for x in client.mappings}
        if "http" in protocols:
            ports.append(str(node.vhost_http_port))
        if "https" in protocols:
            ports.append(str(node.vhost_https_port))
        result = remote.run("ss -lntup", check=False)
        occupied = []
        for port in ports:
            lines = [
                line
                for line in result.stdout.splitlines()
                if f":{port} " in line or line.rstrip().endswith(f":{port}")
            ]
            if lines and (
                not allow_frps or any('users:(("frps"' not in line for line in lines)
            ):
                occupied.append(port)
        checks.append(
            Check(
                "ports-free",
                not occupied,
                "occupied: " + ", ".join(occupied) if occupied else "all free",
            )
        )
        return checks

    def client_checks(
        self, node: Node, client: Client, remote: SSHExecutor
    ) -> list[Check]:
        checks: list[Check] = []
        for command, label in [
            ("uname -s", "client-OS"),
            ("command -v systemctl", "client-systemd"),
            ("command -v ssh", "client-ssh"),
            ("command -v ssh-keygen", "client-ssh-keygen"),
            ("command -v ssh-keyscan", "client-ssh-keyscan"),
        ]:
            result = remote.run(command, check=False)
            checks.append(
                Check(label, result.ok, (result.stdout or result.stderr).strip())
            )
        for command, label in [
            ("command -v curl", "client-curl"),
            ("command -v tar", "client-tar"),
            ("command -v sha256sum", "client-sha256sum"),
        ]:
            result = remote.run(command, check=False)
            checks.append(
                Check(
                    label,
                    True,
                    result.stdout.strip()
                    if result.ok
                    else "missing; will install during deployment",
                )
            )
        listeners = remote.run("ss -lnt", check=False).stdout
        tunnel_busy = any(
            f":{node.tunnel_port} " in line for line in listeners.splitlines()
        )
        checks.append(
            Check(
                "client-tunnel-port",
                not tunnel_busy,
                f"{node.tunnel_port} occupied"
                if tunnel_busy
                else f"{node.tunnel_port} free",
            )
        )
        unavailable = []
        for mapping in client.mappings:
            if mapping.protocol == "tcp" and not any(
                f":{mapping.local_port} " in line for line in listeners.splitlines()
            ):
                unavailable.append(
                    f"{mapping.id}({mapping.local_ip}:{mapping.local_port})"
                )
        checks.append(
            Check(
                "client-targets",
                True,
                "warning unavailable: " + ", ".join(unavailable)
                if unavailable
                else "all TCP targets listening",
            )
        )
        return checks

    def verify_public_listeners(
        self, node: Node, client: Client, remote: SSHExecutor
    ) -> list[Check]:
        ports = [
            effective_remote_port(node, mapping)
            for mapping in client.mappings
            if effective_remote_port(node, mapping) is not None
        ]
        protocols = {mapping.protocol for mapping in client.mappings}
        if "http" in protocols:
            ports.append(node.vhost_http_port)
        if "https" in protocols:
            ports.append(node.vhost_https_port)
        result = None
        for _ in range(15):
            result = remote.run("ss -lntup", check=False)
            if all(
                any(
                    f":{port} " in line
                    and "127.0.0.1:" not in line
                    and "[::1]:" not in line
                    for line in result.stdout.splitlines()
                )
                for port in set(ports)
            ):
                break
            time.sleep(1)
        assert result is not None
        checks: list[Check] = []
        for port in sorted(set(ports)):
            lines = [
                line
                for line in result.stdout.splitlines()
                if f":{port} " in line or line.rstrip().endswith(f":{port}")
            ]
            public = any(
                "127.0.0.1:" not in line and "[::1]:" not in line for line in lines
            )
            checks.append(
                Check(
                    f"public-listener-{port}",
                    public,
                    "publicly bound" if public else "missing or bound to loopback only",
                )
            )
        return checks

    def sync_node(
        self,
        node: Node,
        client: Client,
        node_remote: SSHExecutor,
        client_remote: SSHExecutor,
        *,
        dry_run: bool = False,
    ) -> list[Check]:
        checks = self.checks(node, client, node_remote, allow_frps=True)
        failed = [x for x in checks if not x.ok]
        if failed:
            raise ValidationError(
                "preflight failed: "
                + "; ".join(f"{x.name}: {x.detail}" for x in failed)
            )
        if dry_run:
            return checks
        token = self.token_for(node.id)
        client_config = (
            "/etc/frp/frpc.toml"
            if node.legacy_tunnel
            else f"/etc/frp/clients/{node.id}.toml"
        )
        client_service = (
            "frpc.service" if node.legacy_tunnel else f"frpc@{node.id}.service"
        )
        tunnel_env = f"/etc/frp/tunnels/{node.id}.env"
        node_remote.run(
            "cp -a /etc/frp/frps.toml /etc/frp/frps.toml.frpctl-backup 2>/dev/null || true",
            sudo=True,
        )
        client_remote.run(
            f"cp -a {shlex.quote(client_config)} {shlex.quote(client_config)}.frpctl-backup 2>/dev/null || true",
            sudo=True,
        )
        if not node.legacy_tunnel:
            client_remote.run(
                f"cp -a {shlex.quote(tunnel_env)} {shlex.quote(tunnel_env)}.frpctl-backup 2>/dev/null || true",
                sudo=True,
            )
        try:
            node_remote.write_atomic(
                "/etc/frp/frps.toml", render_frps(node, client, token), mode=0o600
            )
            client_remote.write_atomic(
                client_config,
                render_frpc(node, client, token),
                mode=0o600,
            )
            if not node.legacy_tunnel:
                key, known_hosts = tunnel_key_paths(node)
                client_remote.write_atomic(
                    tunnel_env, tunnel_env_file(node, key, known_hosts), mode=0o600
                )
                client_remote.write_atomic(
                    "/etc/systemd/system/frp-tunnel@.service", TUNNEL_UNIT, mode=0o644
                )
            node_remote.run(
                "/usr/local/bin/frps verify -c /etc/frp/frps.toml; systemctl restart frps.service; systemctl is-active --quiet frps.service",
                sudo=True,
            )
            if not node.legacy_tunnel:
                client_remote.run(
                    f"systemctl daemon-reload; systemctl restart frp-tunnel@{shlex.quote(node.id)}.service; "
                    f"systemctl is-active --quiet frp-tunnel@{shlex.quote(node.id)}.service",
                    sudo=True,
                )
            client_remote.run(
                f"/usr/local/bin/frpc verify -c {shlex.quote(client_config)}; systemctl restart {shlex.quote(client_service)}; systemctl is-active --quiet {shlex.quote(client_service)}",
                sudo=True,
            )
            listener_checks = self.verify_public_listeners(node, client, node_remote)
            failed_listeners = [check for check in listener_checks if not check.ok]
            if failed_listeners:
                raise DeploymentError(
                    "public listener verification failed: "
                    + "; ".join(
                        f"{check.name}: {check.detail}" for check in failed_listeners
                    )
                )
            checks.extend(listener_checks)
        except Exception as exc:
            node_remote.run(
                "test ! -f /etc/frp/frps.toml.frpctl-backup || { mv -f /etc/frp/frps.toml.frpctl-backup /etc/frp/frps.toml; systemctl restart frps.service; }",
                sudo=True,
                check=False,
            )
            if not node.legacy_tunnel:
                client_remote.run(
                    f"test ! -f {shlex.quote(tunnel_env)}.frpctl-backup || {{ mv -f {shlex.quote(tunnel_env)}.frpctl-backup {shlex.quote(tunnel_env)}; "
                    f"systemctl daemon-reload; systemctl restart frp-tunnel@{shlex.quote(node.id)}.service; }}",
                    sudo=True,
                    check=False,
                )
            client_remote.run(
                f"test ! -f {shlex.quote(client_config)}.frpctl-backup || {{ mv -f {shlex.quote(client_config)}.frpctl-backup {shlex.quote(client_config)}; systemctl restart {shlex.quote(client_service)}; }}",
                sudo=True,
                check=False,
            )
            raise DeploymentError(
                f"sync failed and rollback was attempted: {exc}"
            ) from exc
        return checks

    def install_frp(
        self, node: Node, remote: SSHExecutor, binary: str = "frps"
    ) -> None:
        remote.run(
            "command -v curl >/dev/null && command -v tar >/dev/null && command -v sha256sum >/dev/null || { apt-get update; DEBIAN_FRONTEND=noninteractive apt-get install -y curl ca-certificates tar coreutils; }",
            sudo=True,
            timeout=180,
        )
        target = self.release_target(remote)
        version = node.frp_version
        filename = f"frp_{version}_{target}.tar.gz"
        base = release_base(version)
        script = (
            "set -eu; tmp=$(mktemp -d); trap 'rm -rf \"$tmp\"' EXIT; "
            f'cd "$tmp"; curl -fsSL --retry 3 {shlex.quote(base + "/" + filename)} -o {shlex.quote(filename)}; '
            f"curl -fsSL --retry 3 {shlex.quote(base + '/frp_sha256_checksums.txt')} -o checksums; "
            f"grep '  {filename}$' checksums | sha256sum -c -; tar -xzf {shlex.quote(filename)}; "
            f"install -m 0755 frp_{version}_{target}/{binary} /usr/local/bin/{binary}"
        )
        remote.run(script, sudo=True, timeout=180)

    @staticmethod
    def release_target(remote: SSHExecutor) -> str:
        arch = remote.run(
            "dpkg --print-architecture 2>/dev/null || uname -m"
        ).stdout.strip()
        target = ARCH_MAP.get(arch, arch)
        if target not in set(ARCH_MAP.values()):
            raise DeploymentError(f"unsupported remote architecture: {arch}")
        return target

    def download_checks(
        self, node: Node, remote: SSHExecutor, label: str
    ) -> list[Check]:
        """Prove the host can reach the frp release before a long install starts.

        ``install_frp`` allows curl three retries with a 180 second ceiling, so a
        blocked route otherwise looks like a hung deployment. Fetching the small
        checksum file follows the same redirect chain as the release tarball.
        """
        name = f"{label}-frp-download"
        proxy = remote.config.http_proxy
        via = f" via {proxy}" if proxy else ""
        if not remote.run("command -v curl", check=False).ok:
            return [Check(name, True, "curl missing; checked again after installation")]
        url = f"{release_base(node.frp_version)}/frp_sha256_checksums.txt"
        result = remote.run(
            f"curl -fsS --max-time 25 -o /dev/null {shlex.quote(url)}", check=False
        )
        if result.ok:
            return [Check(name, True, f"reachable{via}")]
        reason = result.stderr.strip() or result.stdout.strip() or "no output"
        return [
            Check(
                name,
                False,
                f"curl exited {result.code}{via}: {reason}. Set a proxy for this "
                f"host with: frpctl proxy set <url> --{label} <id>",
            )
        ]

    def deploy_node(
        self, node: Node, client: Client, remote: SSHExecutor, *, dry_run: bool = False
    ) -> list[Check]:
        token = self.token_for(node.id)
        checks = self.checks(node, client, remote)
        failed = [x for x in checks if not x.ok]
        if failed:
            raise ValidationError(
                "preflight failed: "
                + "; ".join(f"{x.name}: {x.detail}" for x in failed)
            )
        if dry_run:
            return checks
        self.install_frp(node, remote, "frps")
        remote.write_atomic("/etc/systemd/system/frps.service", FRPS_UNIT, mode=0o644)
        remote.write_atomic(
            "/etc/frp/frps.toml", render_frps(node, client, token), mode=0o600
        )
        remote.run(
            "/usr/local/bin/frps verify -c /etc/frp/frps.toml; systemctl daemon-reload; systemctl enable --now frps.service",
            sudo=True,
        )
        return checks

    def render_client(self, node: Node, client: Client) -> str:
        return render_frpc(node, client, self.token_for(node.id))

    def install_client(
        self,
        node: Node,
        client: Client,
        client_remote: SSHExecutor,
        node_remote: SSHExecutor,
        *,
        dry_run: bool = False,
    ) -> None:
        """Install a node-specific tunnel key, tunnel unit, and frpc unit on a client."""
        if dry_run:
            return
        self.install_frp(node, client_remote, "frpc")
        key, known_hosts = tunnel_key_paths(node)
        client_remote.run(
            f"mkdir -p /etc/frp-manager/keys /etc/frp/clients /etc/frp/tunnels; if [ ! -f {shlex.quote(key)} ]; then ssh-keygen -q -t ed25519 -N '' -f {shlex.quote(key)}; fi; ssh-keyscan -p {node.ssh.port} {shlex.quote(node.ssh.host)} > {shlex.quote(known_hosts)}; chmod 600 {shlex.quote(key)} {shlex.quote(known_hosts)}",
            sudo=True,
        )
        pub = client_remote.run(f"cat {shlex.quote(key)}.pub", sudo=True).stdout.strip()
        restricted = f'restrict,port-forwarding,permitopen="127.0.0.1:{node.frps_bind_port}" {pub} frpctl-{node.id}'
        node_remote.run(
            "mkdir -p ~/.ssh; chmod 700 ~/.ssh; touch ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys",
            sudo=False,
        )
        node_remote.run(
            "grep -F "
            + shlex.quote(f"frpctl-{node.id}")
            + " ~/.ssh/authorized_keys >/dev/null 2>&1 || echo "
            + shlex.quote(restricted)
            + " >> ~/.ssh/authorized_keys",
            sudo=False,
        )
        client_remote.write_atomic(
            f"/etc/frp/tunnels/{node.id}.env",
            tunnel_env_file(node, key, known_hosts),
            mode=0o600,
        )
        client_remote.write_atomic(
            "/etc/systemd/system/frp-tunnel@.service", TUNNEL_UNIT, mode=0o644
        )
        client_remote.write_atomic(
            "/etc/systemd/system/frpc@.service", FRPC_UNIT, mode=0o644
        )
        client_remote.write_atomic(
            f"/etc/frp/clients/{node.id}.toml",
            self.render_client(node, client),
            mode=0o600,
        )
        client_remote.run(
            f"/usr/local/bin/frpc verify -c /etc/frp/clients/{shlex.quote(node.id)}.toml; systemctl daemon-reload; systemctl enable --now frp-tunnel@{shlex.quote(node.id)}.service; systemctl enable --now frpc@{shlex.quote(node.id)}.service",
            sudo=True,
        )

    def rollback_new_node(
        self,
        node: Node,
        node_remote: SSHExecutor,
        client_remote: SSHExecutor | None = None,
    ) -> None:
        if client_remote is not None:
            client_remote.run(
                f"systemctl disable --now frpc@{shlex.quote(node.id)}.service frp-tunnel@{shlex.quote(node.id)}.service 2>/dev/null || true; "
                f"rm -f /etc/frp/clients/{shlex.quote(node.id)}.toml /etc/frp/tunnels/{shlex.quote(node.id)}.env "
                f"/etc/frp-manager/keys/{shlex.quote(node.id)}_ed25519 /etc/frp-manager/keys/{shlex.quote(node.id)}_ed25519.pub "
                f"/etc/frp-manager/keys/{shlex.quote(node.id)}_known_hosts; systemctl daemon-reload",
                sudo=True,
                check=False,
            )
        node_remote.run(
            "systemctl disable --now frps.service 2>/dev/null || true; rm -f /etc/frp/frps.toml /etc/systemd/system/frps.service; systemctl daemon-reload",
            sudo=True,
            check=False,
        )
        node_remote.run(
            f"test ! -f ~/.ssh/authorized_keys || sed -i '/frpctl-{shlex.quote(node.id)}$/d' ~/.ssh/authorized_keys",
            check=False,
        )
