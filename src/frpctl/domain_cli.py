from __future__ import annotations

import copy
import getpass
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .domains import (
    DomainBinding,
    DomainManager,
    DomainStore,
    relocate_listener_port,
    render_nginx,
)
from .errors import FrpCtlError
from .inventory import InventoryStore
from .service import Credentials

app = typer.Typer(
    no_args_is_help=True, help="Manage per-node domain and HTTPS bindings."
)
console = Console()


def stores(inventory: Path, domains: Path) -> tuple[InventoryStore, DomainStore]:
    return InventoryStore(inventory, inventory.with_name("secrets.yaml")), DomainStore(
        domains
    )


@app.command("add")
def add(
    hostname: str,
    port: int = typer.Option(..., "--port", help="Existing HTTP FRP port to proxy."),
    node_id: str = typer.Option(..., "--node"),
    inventory: Path = typer.Option(
        Path("/etc/frp-manager/inventory.yaml"), "--inventory"
    ),
    domains: Path = typer.Option(Path("/etc/frp-manager/domains.yaml"), "--domains"),
) -> None:
    inventory_store, domain_store = stores(inventory, domains)
    inventory_store.load().node(node_id)
    desired = domain_store.load()
    node = desired.node(node_id, create=True)
    normalized = hostname.lower().rstrip(".")
    existing = next(
        (item for item in node.bindings if item.hostname == normalized), None
    )
    if existing:
        existing.upstream_port = port
    else:
        node.bindings.append(DomainBinding(normalized, port))
    domain_store.save(desired)
    console.print(f"Saved {normalized} -> {node_id}:{port}")


@app.command("remove")
def remove(
    hostname: str,
    node_id: str = typer.Option(..., "--node"),
    domains: Path = typer.Option(Path("/etc/frp-manager/domains.yaml"), "--domains"),
) -> None:
    store = DomainStore(domains)
    desired = store.load()
    node = desired.node(node_id)
    normalized = hostname.lower().rstrip(".")
    node.bindings = [item for item in node.bindings if item.hostname != normalized]
    store.save(desired)
    console.print(f"Removed {normalized}")


@app.command("list")
def list_bindings(
    node_id: str | None = typer.Option(None, "--node"),
    domains: Path = typer.Option(Path("/etc/frp-manager/domains.yaml"), "--domains"),
) -> None:
    desired = DomainStore(domains).load()
    table = Table("Node", "Domain", "Upstream", "HTTPS email")
    for node in desired.nodes:
        if node_id and node.node_id != node_id:
            continue
        for binding in node.bindings:
            table.add_row(
                node.node_id,
                binding.hostname,
                str(binding.upstream_port),
                node.email or "-",
            )
    console.print(table)


@app.command("dns")
def dns(
    node_id: str = typer.Option(..., "--node"),
    inventory: Path = typer.Option(
        Path("/etc/frp-manager/inventory.yaml"), "--inventory"
    ),
    domains: Path = typer.Option(Path("/etc/frp-manager/domains.yaml"), "--domains"),
) -> None:
    inventory_store, domain_store = stores(inventory, domains)
    node = inventory_store.load().node(node_id)
    config = domain_store.load().node(node_id)
    table = Table("Type", "Name", "Value")
    for binding in config.bindings:
        table.add_row("A", binding.hostname, node.ssh.host)
    console.print(table)


@app.command("plan")
def plan(
    node_id: str = typer.Option(..., "--node"),
    client_id: str = typer.Option("client-109", "--client"),
    inventory: Path = typer.Option(
        Path("/etc/frp-manager/inventory.yaml"), "--inventory"
    ),
    domains: Path = typer.Option(Path("/etc/frp-manager/domains.yaml"), "--domains"),
) -> None:
    inventory_store, domain_store = stores(inventory, domains)
    desired = copy.deepcopy(inventory_store.load())
    node, client = desired.node(node_id), desired.client(client_id)
    old_overrides = dict(node.port_overrides)
    relocated = relocate_listener_port(desired, node, client)
    config = domain_store.load().node(node_id)
    if node.port_overrides != old_overrides:
        console.print(f"FRP port 80 will move to {relocated} on {node_id} only.")
    console.rule("Final Nginx configuration")
    console.print(render_nginx(node, client, config, tls=True), markup=False)


@app.command("apply")
def apply(
    node_id: str = typer.Option(..., "--node"),
    email: str = typer.Option(..., "--email", prompt=True),
    client_id: str = typer.Option("client-109", "--client"),
    skip_dns_check: bool = typer.Option(False, "--skip-dns-check"),
    edge_proxy: bool = typer.Option(
        False,
        "--edge-proxy",
        help="Allow DNS to resolve to an ESA/CDN edge IP instead of the origin IP.",
    ),
    inventory: Path = typer.Option(
        Path("/etc/frp-manager/inventory.yaml"), "--inventory"
    ),
    domains: Path = typer.Option(Path("/etc/frp-manager/domains.yaml"), "--domains"),
) -> None:
    inventory_store, domain_store = stores(inventory, domains)
    desired = inventory_store.load()
    node, client = desired.node(node_id), desired.client(client_id)
    node_password = (
        None
        if node.ssh.key_file
        else getpass.getpass(f"SSH password for {node.ssh.user}@{node.ssh.host}: ")
    )
    node_sudo = getpass.getpass("node sudo password (empty if not required): ") or None
    client_password = (
        None
        if client.ssh.key_file
        else getpass.getpass(f"SSH password for {client.ssh.user}@{client.ssh.host}: ")
    )
    client_sudo = (
        getpass.getpass("client sudo password (empty if not required): ") or None
    )
    DomainManager(inventory_store, domain_store).apply(
        node_id,
        client_id,
        email,
        Credentials(node_password, node_sudo),
        Credentials(client_password, client_sudo),
        skip_dns_check=skip_dns_check,
        edge_proxy=edge_proxy,
    )
    console.print(f"Configured HTTP/HTTPS bindings for {node_id}")


@app.command("tui")
def tui(
    inventory: Path = typer.Option(
        Path("/etc/frp-manager/inventory.yaml"), "--inventory"
    ),
    domains: Path = typer.Option(Path("/etc/frp-manager/domains.yaml"), "--domains"),
) -> None:
    """Browse and edit domain bindings interactively."""
    from .domain_tui import FrpDomainApp

    FrpDomainApp(inventory, domains).run()


def main() -> int:
    try:
        app()
    except FrpCtlError as exc:
        console.print(f"[red]error:[/red] {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
