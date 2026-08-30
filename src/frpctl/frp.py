from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

from .models import Client, Mapping, Node


def quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def effective_remote_port(node: Node, mapping: Mapping) -> int | None:
    return node.port_overrides.get(mapping.id, mapping.remote_port)


def render_frps(node: Node, client: Client, token: str) -> str:
    ports = sorted(
        effective_remote_port(node, mapping)
        for mapping in client.mappings
        if mapping.protocol in {"tcp", "udp"}
        and effective_remote_port(node, mapping) is not None
    )
    allow = ", ".join(f"{{ single = {port} }}" for port in ports)
    lines = [
        'bindAddr = "127.0.0.1"',
        f"bindPort = {node.frps_bind_port}",
        'proxyBindAddr = "0.0.0.0"',
        'auth.method = "token"',
        f"auth.token = {quote(token)}",
    ]
    if allow:
        lines.append(f"allowPorts = [{allow}]")
    protocols = {mapping.protocol for mapping in client.mappings}
    if "http" in protocols:
        lines.append(f"vhostHTTPPort = {node.vhost_http_port}")
    if "https" in protocols:
        lines.append(f"vhostHTTPSPort = {node.vhost_https_port}")
    return "\n".join(lines) + "\n"


def render_frpc(node: Node, client: Client, token: str) -> str:
    lines = [
        'serverAddr = "127.0.0.1"',
        f"serverPort = {node.tunnel_port}",
        "loginFailExit = false",
        'auth.method = "token"',
        f"auth.token = {quote(token)}",
        "transport.tls.enable = true",
    ]
    for mapping in client.mappings:
        lines.extend(
            [
                "",
                "[[proxies]]",
                f"name = {quote(mapping.id)}",
                f"type = {quote(mapping.protocol)}",
            ]
        )
        lines.extend(
            [
                f"localIP = {quote(mapping.local_ip)}",
                f"localPort = {mapping.local_port}",
            ]
        )
        remote_port = effective_remote_port(node, mapping)
        if remote_port is not None:
            lines.append(f"remotePort = {remote_port}")
        if mapping.custom_domains:
            domains = ", ".join(quote(x) for x in mapping.custom_domains)
            lines.append(f"customDomains = [{domains}]")
        if mapping.locations:
            locations = ", ".join(quote(x) for x in mapping.locations)
            lines.append(f"locations = [{locations}]")
        for key, value in mapping.extra.items():
            if isinstance(value, bool):
                rendered = str(value).lower()
            elif isinstance(value, int):
                rendered = str(value)
            elif isinstance(value, list):
                rendered = "[" + ", ".join(quote(str(x)) for x in value) + "]"
            else:
                rendered = quote(str(value))
            lines.append(f"{key} = {rendered}")
    return "\n".join(lines) + "\n"


def import_frpc(path: str | Path) -> tuple[dict[str, Any], list[Mapping]]:
    with Path(path).open("rb") as handle:
        data = tomllib.load(handle)
    return import_frpc_data(data)


def import_frpc_text(text: str) -> tuple[dict[str, Any], list[Mapping]]:
    return import_frpc_data(tomllib.loads(text))


def import_frpc_data(data: dict[str, Any]) -> tuple[dict[str, Any], list[Mapping]]:
    mappings: list[Mapping] = []
    for proxy in data.get("proxies", []):
        known = {
            "name",
            "type",
            "localIP",
            "localPort",
            "remotePort",
            "customDomains",
            "locations",
        }
        mappings.append(
            Mapping(
                id=proxy["name"],
                protocol=proxy["type"],
                local_ip=proxy.get("localIP", "127.0.0.1"),
                local_port=int(proxy["localPort"]),
                remote_port=proxy.get("remotePort"),
                custom_domains=list(proxy.get("customDomains", [])),
                locations=list(proxy.get("locations", [])),
                extra={key: value for key, value in proxy.items() if key not in known},
            )
        )
    metadata = {
        "server_addr": data.get("serverAddr"),
        "server_port": data.get("serverPort"),
        "has_token": bool(data.get("auth", {}).get("token")),
        "token": data.get("auth", {}).get("token"),
    }
    return metadata, mappings


def redact_toml(text: str) -> str:
    return re.sub(
        r"(?im)^(.*(?:token|password|secret|credential).*?=).*$",
        r'\1 "<redacted>"',
        text,
    )
