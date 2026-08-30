from __future__ import annotations

import copy
import re
import secrets
import shlex
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import DeploymentError, FrpCtlError, ValidationError
from .frp import effective_remote_port
from .inventory import InventoryStore
from .models import Client, Inventory, Node
from .service import Credentials, Manager
from .ssh import SSHExecutor

HOSTNAME = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$"
)


@dataclass(slots=True)
class DomainBinding:
    hostname: str
    upstream_port: int

    def validate(self) -> None:
        self.hostname = self.hostname.lower().rstrip(".")
        if not HOSTNAME.fullmatch(self.hostname):
            raise ValidationError(f"invalid hostname: {self.hostname}")
        if not 1 <= self.upstream_port <= 65535:
            raise ValidationError(f"invalid upstream port: {self.upstream_port}")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DomainBinding:
        binding = cls(**value)
        binding.validate()
        return binding


@dataclass(slots=True)
class NodeDomains:
    node_id: str
    email: str | None = None
    fallback_port: int = 80
    bindings: list[DomainBinding] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> NodeDomains:
        data = dict(value)
        data["bindings"] = [
            DomainBinding.from_dict(x) for x in data.get("bindings", [])
        ]
        return cls(**data)

    def validate(self) -> None:
        if not 1 <= self.fallback_port <= 65535:
            raise ValidationError(f"invalid fallback port: {self.fallback_port}")
        if self.email is not None and (
            "@" not in self.email
            or self.email.startswith("@")
            or self.email.endswith("@")
        ):
            raise ValidationError(f"invalid certificate email: {self.email}")
        names: set[str] = set()
        for binding in self.bindings:
            binding.validate()
            if binding.hostname in names:
                raise ValidationError(f"duplicate hostname: {binding.hostname}")
            names.add(binding.hostname)


@dataclass(slots=True)
class DomainInventory:
    version: int = 1
    nodes: list[NodeDomains] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> DomainInventory:
        value = value or {}
        result = cls(
            version=int(value.get("version", 1)),
            nodes=[NodeDomains.from_dict(x) for x in value.get("nodes", [])],
        )
        result.validate()
        return result

    def validate(self) -> None:
        ids = [node.node_id for node in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValidationError("duplicate domain node id")
        for node in self.nodes:
            node.validate()

    def node(self, node_id: str, *, create: bool = False) -> NodeDomains:
        found = next((node for node in self.nodes if node.node_id == node_id), None)
        if found:
            return found
        if create:
            found = NodeDomains(node_id)
            self.nodes.append(found)
            return found
        raise ValidationError(f"no domain configuration for node: {node_id}")


class DomainStore:
    def __init__(self, path: str | Path = "/etc/frp-manager/domains.yaml"):
        self.path = Path(path)

    def load(self) -> DomainInventory:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                return DomainInventory.from_dict(yaml.safe_load(handle))
        except FileNotFoundError:
            return DomainInventory()
        except PermissionError as exc:
            raise FrpCtlError(
                f"cannot read {self.path}; run frpdomain with sudo or choose a readable --domains path"
            ) from exc

    def save(self, inventory: DomainInventory) -> None:
        inventory.validate()
        InventoryStore._atomic_yaml(self.path, asdict(inventory))


def choose_relocation_port(
    inventory: Inventory, node: Node, client: Client, start: int = 10080
) -> int:
    used = {
        effective_remote_port(node, mapping)
        for mapping in client.mappings
        if effective_remote_port(node, mapping) is not None
    }
    while start in used or start in {node.frps_bind_port, 80, 443}:
        start += 1
    return start


def relocate_listener_port(
    inventory: Inventory, node: Node, client: Client, port: int = 80
) -> int:
    mappings = [
        mapping
        for mapping in client.mappings
        if mapping.protocol == "tcp" and effective_remote_port(node, mapping) == port
    ]
    if not mappings:
        return port
    if len(mappings) != 1:
        raise ValidationError(
            f"expected one TCP mapping on port {port}, found {len(mappings)}"
        )
    mapping = mappings[0]
    relocated = node.port_overrides.get(mapping.id) or choose_relocation_port(
        inventory, node, client
    )
    node.port_overrides[mapping.id] = relocated
    inventory.validate()
    return relocated


def resolved_upstream(node: Node, client: Client, requested_port: int) -> int:
    mapping = next(
        (
            item
            for item in client.mappings
            if item.remote_port == requested_port and item.id in node.port_overrides
        ),
        None,
    )
    return node.port_overrides[mapping.id] if mapping else requested_port


def proxy_block(upstream_port: int) -> str:
    return f"""        proxy_pass http://127.0.0.1:{upstream_port};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
"""


def render_nginx(node: Node, client: Client, config: NodeDomains, *, tls: bool) -> str:
    fallback = resolved_upstream(node, client, config.fallback_port)
    cert_name = f"frpdomain-{node.id}"
    lines = [
        "map $http_upgrade $connection_upgrade {",
        "    default upgrade;",
        "    '' close;",
        "}",
        "",
        "server {",
        "    listen 80 default_server;",
        "    listen [::]:80 default_server;",
        "    server_name _;",
        "    location ^~ /.well-known/acme-challenge/ { root /var/www/certbot; }",
        "    location / {",
        proxy_block(fallback).rstrip(),
        "    }",
        "}",
    ]
    for binding in config.bindings:
        upstream = resolved_upstream(node, client, binding.upstream_port)
        lines.extend(
            [
                "",
                "server {",
                "    listen 80;",
                "    listen [::]:80;",
                f"    server_name {binding.hostname};",
                "    location ^~ /.well-known/acme-challenge/ { root /var/www/certbot; }",
                "    location / {",
            ]
        )
        if tls:
            lines.append("        return 301 https://$host$request_uri;")
        else:
            lines.append(proxy_block(upstream).rstrip())
        lines.extend(["    }", "}"])
        if tls:
            lines.extend(
                [
                    "",
                    "server {",
                    "    listen 443 ssl http2;",
                    "    listen [::]:443 ssl http2;",
                    f"    server_name {binding.hostname};",
                    f"    ssl_certificate /etc/letsencrypt/live/{cert_name}/fullchain.pem;",
                    f"    ssl_certificate_key /etc/letsencrypt/live/{cert_name}/privkey.pem;",
                    "    location / {",
                    proxy_block(upstream).rstrip(),
                    "    }",
                    "}",
                ]
            )
    return "\n".join(lines) + "\n"


class DomainManager:
    def __init__(self, inventory_store: InventoryStore, domain_store: DomainStore):
        self.inventory_store = inventory_store
        self.domain_store = domain_store

    def apply(
        self,
        node_id: str,
        client_id: str,
        email: str,
        node_credentials: Credentials,
        client_credentials: Credentials,
        *,
        skip_dns_check: bool = False,
        edge_proxy: bool = False,
    ) -> None:
        inventory = self.inventory_store.load()
        original = copy.deepcopy(inventory)
        domains = self.domain_store.load()
        config = domains.node(node_id)
        config.email = email
        config.validate()
        node, client = inventory.node(node_id), inventory.client(client_id)
        with SSHExecutor(
            node.ssh, node_credentials.password, node_credentials.sudo_password
        ) as remote:
            self._check_dns(remote, node, config, skip_dns_check, edge_proxy)
        relocate_listener_port(inventory, node, client, 80)
        self.inventory_store.save(inventory)
        try:
            Manager(self.inventory_store).sync_node(
                node_id,
                client_id,
                node_credentials,
                client_credentials,
            )
            with SSHExecutor(
                node.ssh, node_credentials.password, node_credentials.sudo_password
            ) as remote:
                self._deploy_nginx(
                    remote, node, client, config, skip_dns_check, edge_proxy
                )
            self.domain_store.save(domains)
        except Exception as exc:
            self.inventory_store.save(original)
            rollback_error: Exception | None = None
            try:
                with SSHExecutor(
                    node.ssh,
                    node_credentials.password,
                    node_credentials.sudo_password,
                ) as remote:
                    self._remove_nginx_site(remote, node)
            except Exception as cleanup_exc:
                rollback_error = cleanup_exc
            try:
                Manager(self.inventory_store).sync_node(
                    node_id,
                    client_id,
                    node_credentials,
                    client_credentials,
                )
            except Exception as rollback_exc:
                rollback_error = (
                    rollback_exc
                    if rollback_error is None
                    else DeploymentError(
                        f"nginx cleanup failed: {rollback_error}; FRP rollback failed: {rollback_exc}"
                    )
                )
            rollback_detail = (
                f"; FRP rollback also failed: {rollback_error}"
                if rollback_error
                else ""
            )
            raise DeploymentError(
                f"domain deployment failed; FRP desired state was restored: {exc}{rollback_detail}"
            ) from exc

    @staticmethod
    def _remove_nginx_site(remote: SSHExecutor, node: Node) -> None:
        remote.run(
            f"rm -f /etc/nginx/sites-enabled/frpdomain-{shlex.quote(node.id)}.conf /etc/nginx/sites-available/frpdomain-{shlex.quote(node.id)}.conf; "
            "systemctl stop nginx 2>/dev/null || true; "
            "rm -f /etc/nginx/sites-enabled/default /etc/nginx/sites-enabled/default.frpdomain-disabled",
            sudo=True,
            check=False,
        )

    @staticmethod
    def _check_dns(
        remote: SSHExecutor,
        node: Node,
        config: NodeDomains,
        skip_dns_check: bool,
        edge_proxy: bool,
    ) -> None:
        hostnames = [binding.hostname for binding in config.bindings]
        if not hostnames:
            raise ValidationError("at least one domain binding is required")
        if not skip_dns_check:
            for hostname in hostnames:
                addresses = remote.run(
                    f"getent ahostsv4 {shlex.quote(hostname)} | awk '{{print $1}}' | sort -u",
                    check=False,
                ).stdout.split()
                if not addresses or (not edge_proxy and node.ssh.host not in addresses):
                    raise ValidationError(
                        f"DNS for {hostname} does not resolve to {node.ssh.host}: {addresses or 'no A record'}"
                    )

    @staticmethod
    def _deploy_nginx(
        remote: SSHExecutor,
        node: Node,
        client: Client,
        config: NodeDomains,
        skip_dns_check: bool,
        edge_proxy: bool,
    ) -> None:
        hostnames = [binding.hostname for binding in config.bindings]
        remote.run(
            "apt-get update; DEBIAN_FRONTEND=noninteractive apt-get install -y nginx certbot",
            sudo=True,
            timeout=300,
        )
        remote.run("mkdir -p /var/www/certbot", sudo=True)
        site = f"/etc/nginx/sites-available/frpdomain-{node.id}.conf"
        enabled = f"/etc/nginx/sites-enabled/frpdomain-{node.id}.conf"
        remote.write_atomic(
            site, render_nginx(node, client, config, tls=False), mode=0o644
        )
        remote.run(
            "mkdir -p /etc/frp-manager/backups; "
            "if [ -e /etc/nginx/sites-enabled/default ]; then mv -f /etc/nginx/sites-enabled/default /etc/frp-manager/backups/nginx-default-site; fi; "
            "if [ -e /etc/nginx/sites-enabled/default.frpdomain-disabled ]; then mv -f /etc/nginx/sites-enabled/default.frpdomain-disabled /etc/frp-manager/backups/nginx-default-site; fi; "
            f"ln -sfn {shlex.quote(site)} {shlex.quote(enabled)}; nginx -t; systemctl enable --now nginx; systemctl reload nginx",
            sudo=True,
        )
        probe_name = f"frpdomain-{secrets.token_hex(8)}"
        probe_value = secrets.token_urlsafe(24)
        probe_dir = "/var/www/certbot/.well-known/acme-challenge"
        remote.run(f"mkdir -p {probe_dir}", sudo=True)
        remote.write_atomic(f"{probe_dir}/{probe_name}", probe_value + "\n", mode=0o644)
        for hostname in hostnames:
            response = remote.run(
                f"curl -fsSL --max-time 20 {shlex.quote(f'http://{hostname}/.well-known/acme-challenge/{probe_name}')}",
                check=False,
                # The probe must travel the public DNS/edge path back to this
                # Nginx; an outbound proxy would validate the wrong route.
                proxy=False,
            )
            if not response.ok or response.stdout.strip() != probe_value:
                raise ValidationError(
                    f"ACME challenge did not reach this Nginx through DNS/ESA for {hostname}; configure ESA origin to {node.ssh.host}:80 and preserve the Host header"
                )
        remote.run(f"rm -f {probe_dir}/{shlex.quote(probe_name)}", sudo=True)
        cert_name = f"frpdomain-{node.id}"
        domains = " ".join(f"-d {shlex.quote(hostname)}" for hostname in hostnames)
        remote.run(
            f"certbot certonly --webroot -w /var/www/certbot --cert-name {shlex.quote(cert_name)} {domains} --email {shlex.quote(config.email or '')} --agree-tos --non-interactive --keep-until-expiring --expand",
            sudo=True,
            timeout=300,
        )
        remote.write_atomic(
            site, render_nginx(node, client, config, tls=True), mode=0o644
        )
        hook = "#!/bin/sh\n/usr/bin/systemctl reload nginx\n"
        remote.write_atomic(
            "/etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh",
            hook,
            mode=0o755,
        )
        remote.run(
            "nginx -t; systemctl reload nginx; systemctl enable --now certbot.timer 2>/dev/null || true",
            sudo=True,
        )
