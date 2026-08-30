from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from .errors import ValidationError

Protocol = Literal["tcp", "udp", "http", "https"]
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
# Schemes curl understands for http_proxy/https_proxy/all_proxy.
PROXY_SCHEMES = (
    "http://",
    "https://",
    "socks4://",
    "socks4a://",
    "socks5://",
    "socks5h://",
)


def validate_id(kind: str, value: str) -> None:
    if not SAFE_ID.fullmatch(value):
        raise ValidationError(f"invalid {kind} id: {value!r}")


def validate_proxy(kind: str, value: str | None) -> None:
    """Reject proxy URLs that a remote shell could not use."""
    if value is None:
        return
    if not value or any(character.isspace() for character in value):
        raise ValidationError(f"invalid {kind} proxy: {value!r}")
    if not value.startswith(PROXY_SCHEMES):
        raise ValidationError(
            f"invalid {kind} proxy: {value!r}; expected a URL starting with "
            + ", ".join(PROXY_SCHEMES)
        )


@dataclass(slots=True)
class SSHConfig:
    host: str
    user: str
    port: int = 22
    key_file: str | None = None
    proxy_jump: str | None = None
    sudo: bool = True
    http_proxy: str | None = None
    """Proxy that this host itself uses for outbound HTTP(S).

    It is exported inside every remote command, so the value is resolved on the
    target machine: ``http://127.0.0.1:7890`` means the proxy running there, not
    one reachable from the controller.
    """

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SSHConfig:
        return cls(**value)


@dataclass(slots=True)
class Mapping:
    id: str
    protocol: Protocol
    local_ip: str
    local_port: int
    remote_port: int | None = None
    custom_domains: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        validate_id("mapping", self.id)
        if self.protocol not in {"tcp", "udp", "http", "https"}:
            raise ValidationError(
                f"unsupported protocol for {self.id}: {self.protocol}"
            )
        if not 1 <= self.local_port <= 65535:
            raise ValidationError(
                f"invalid local port for {self.id}: {self.local_port}"
            )
        if self.protocol in {"tcp", "udp"}:
            if self.remote_port is None or not 1 <= self.remote_port <= 65535:
                raise ValidationError(f"{self.id} requires a valid remote_port")
        elif not self.custom_domains:
            raise ValidationError(
                f"{self.id} requires custom_domains for {self.protocol}"
            )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Mapping:
        item = cls(**value)
        item.validate()
        return item


@dataclass(slots=True)
class Client:
    id: str
    ssh: SSHConfig
    mappings: list[Mapping] = field(default_factory=list)
    node_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Client:
        data = dict(value)
        data["ssh"] = SSHConfig.from_dict(data["ssh"])
        data["mappings"] = [Mapping.from_dict(x) for x in data.get("mappings", [])]
        return cls(**data)


@dataclass(slots=True)
class Node:
    id: str
    ssh: SSHConfig
    tunnel_port: int
    frp_version: str = "0.70.0"
    frps_bind_port: int = 7000
    vhost_http_port: int = 80
    vhost_https_port: int = 443
    port_overrides: dict[str, int] = field(default_factory=dict)
    managed: bool = True
    legacy_tunnel: bool = False

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Node:
        data = dict(value)
        data["ssh"] = SSHConfig.from_dict(data["ssh"])
        return cls(**data)


@dataclass(slots=True)
class Inventory:
    version: int = 1
    clients: list[Client] = field(default_factory=list)
    nodes: list[Node] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> Inventory:
        value = value or {}
        inv = cls(
            version=int(value.get("version", 1)),
            clients=[Client.from_dict(x) for x in value.get("clients", [])],
            nodes=[Node.from_dict(x) for x in value.get("nodes", [])],
        )
        inv.validate()
        return inv

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def validate(self) -> None:
        client_ids = [x.id for x in self.clients]
        node_ids = [x.id for x in self.nodes]
        if len(client_ids) != len(set(client_ids)):
            raise ValidationError("duplicate client id")
        if len(node_ids) != len(set(node_ids)):
            raise ValidationError("duplicate node id")
        known_nodes = set(node_ids)
        tunnel_ports = [x.tunnel_port for x in self.nodes]
        if len(tunnel_ports) != len(set(tunnel_ports)):
            raise ValidationError("duplicate local tunnel port")
        for client in self.clients:
            validate_id("client", client.id)
            validate_proxy(f"client {client.id}", client.ssh.http_proxy)
            unknown = set(client.node_ids) - known_nodes
            if unknown:
                raise ValidationError(
                    f"client {client.id} references unknown nodes: {sorted(unknown)}"
                )
            mapping_ids = [x.id for x in client.mappings]
            if len(mapping_ids) != len(set(mapping_ids)):
                raise ValidationError(f"duplicate mapping id on client {client.id}")
            ports: set[tuple[str, int]] = set()
            for mapping in client.mappings:
                mapping.validate()
                if mapping.remote_port is not None:
                    key = (mapping.protocol, mapping.remote_port)
                    if key in ports:
                        raise ValidationError(
                            f"duplicate {mapping.protocol} remote port {mapping.remote_port} on {client.id}"
                        )
                    ports.add(key)
        for node in self.nodes:
            validate_id("node", node.id)
            validate_proxy(f"node {node.id}", node.ssh.http_proxy)
            if not 1024 <= node.tunnel_port <= 65535:
                raise ValidationError(
                    f"invalid tunnel port for {node.id}: {node.tunnel_port}"
                )
            for mapping_id, port in node.port_overrides.items():
                validate_id("mapping override", mapping_id)
                if not 1 <= port <= 65535:
                    raise ValidationError(
                        f"invalid overridden port for {node.id}/{mapping_id}: {port}"
                    )
        for client in self.clients:
            protocols = {x.protocol for x in client.mappings}
            for node_id in client.node_ids:
                node = next((x for x in self.nodes if x.id == node_id), None)
                if not node:
                    continue
                known_mappings = {mapping.id for mapping in client.mappings}
                unknown_overrides = set(node.port_overrides) - known_mappings
                if unknown_overrides:
                    raise ValidationError(
                        f"node {node.id} overrides unknown mappings: {sorted(unknown_overrides)}"
                    )
                tcp_ports = {
                    node.port_overrides.get(x.id, x.remote_port)
                    for x in client.mappings
                    if x.protocol == "tcp"
                }
                effective_ports = [
                    node.port_overrides.get(x.id, x.remote_port)
                    for x in client.mappings
                    if x.protocol in {"tcp", "udp"} and x.remote_port is not None
                ]
                if len(effective_ports) != len(set(effective_ports)):
                    raise ValidationError(
                        f"node {node.id} has duplicate effective remote ports"
                    )
                if "http" in protocols and node.vhost_http_port in tcp_ports:
                    raise ValidationError(
                        f"client {client.id}: HTTP vhost port {node.vhost_http_port} conflicts with a TCP mapping"
                    )
                if "https" in protocols and node.vhost_https_port in tcp_ports:
                    raise ValidationError(
                        f"client {client.id}: HTTPS vhost port {node.vhost_https_port} conflicts with a TCP mapping"
                    )

    def client(self, client_id: str) -> Client:
        try:
            return next(x for x in self.clients if x.id == client_id)
        except StopIteration as exc:
            raise ValidationError(f"unknown client: {client_id}") from exc

    def node(self, node_id: str) -> Node:
        try:
            return next(x for x in self.nodes if x.id == node_id)
        except StopIteration as exc:
            raise ValidationError(f"unknown node: {node_id}") from exc

    def next_tunnel_port(self, start: int = 17000) -> int:
        used = {x.tunnel_port for x in self.nodes}
        while start in used:
            start += 1
        return start
