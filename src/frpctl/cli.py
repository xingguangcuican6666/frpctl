from __future__ import annotations

import getpass
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .errors import FrpCtlError
from .frp import import_frpc, redact_toml, render_frpc, render_frps
from .inventory import InventoryStore
from .models import Client, Inventory, Mapping, Node, SSHConfig
from .service import Credentials, Manager

app = typer.Typer(
    no_args_is_help=True, help="Manage mirrored FRP mappings across public nodes."
)
node_app = typer.Typer(help="Manage public FRP nodes.")
mapping_app = typer.Typer(help="Manage mappings.")
proxy_app = typer.Typer(help="Manage the outbound proxy each host uses itself.")
app.add_typer(node_app, name="node")
app.add_typer(mapping_app, name="mapping")
app.add_typer(proxy_app, name="proxy")
console = Console()


def host_ssh(inv: Inventory, node_id: str | None, client_id: str | None) -> SSHConfig:
    """Resolve exactly one node or client to operate on."""
    if bool(node_id) == bool(client_id):
        raise FrpCtlError("choose exactly one of --node ID or --client ID")
    return inv.node(node_id).ssh if node_id else inv.client(client_id).ssh


def store(path: Path, secrets: Path | None) -> InventoryStore:
    return InventoryStore(path, secrets or path.with_name("secrets.yaml"))


def fail(exc: Exception) -> None:
    console.print(f"[red]error:[/red] {exc}")
    raise typer.Exit(1)


@app.command("init")
def init(
    inventory: Path = typer.Option(
        Path("/etc/frp-manager/inventory.yaml"), "--inventory"
    ),
) -> None:
    target = store(inventory, None)
    if inventory.exists():
        fail(FrpCtlError(f"inventory already exists: {inventory}"))
    target.save(Inventory(), {"nodes": {}})
    console.print(f"Created [green]{inventory}[/green]")


@app.command("import")
def import_existing(
    frpc: Path = typer.Option(Path("/etc/frp/frpc.toml"), exists=True, readable=True),
    client_id: str = typer.Option("client-109"),
    client_host: str = typer.Option("192.168.1.109"),
    client_user: str = typer.Option("deploy"),
    node_id: str = typer.Option("node-123"),
    node_host: str = typer.Option("203.0.113.123"),
    node_user: str = typer.Option("root"),
    tunnel_port: int = typer.Option(17000),
    inventory: Path = typer.Option(
        Path("/etc/frp-manager/inventory.yaml"), "--inventory"
    ),
    apply: bool = typer.Option(
        False, "--apply", help="Write the imported desired state."
    ),
) -> None:
    metadata, mappings = import_frpc(frpc)
    inv = Inventory(
        clients=[
            Client(client_id, SSHConfig(client_host, client_user), mappings, [node_id])
        ],
        nodes=[
            Node(
                node_id,
                SSHConfig(node_host, node_user),
                tunnel_port,
                legacy_tunnel=True,
            )
        ],
    )
    inv.validate()
    console.print(
        f"Import preview: {len(mappings)} mappings; server={metadata['server_addr']}:{metadata['server_port']}"
    )
    for item in mappings:
        console.print(
            f"  {item.id}: {item.protocol} {item.local_ip}:{item.local_port} -> {item.remote_port or item.custom_domains}"
        )
    if apply:
        secrets_data = {"nodes": {node_id: {"token": metadata.get("token")}}}
        store(inventory, None).save(inv, secrets_data)
        console.print(f"Imported into [green]{inventory}[/green]")
    else:
        console.print("Dry preview only; pass --apply to save.")


@node_app.command("add")
def node_add(
    host: str,
    user: str = typer.Option(..., prompt=True),
    node_id: str | None = typer.Option(None),
    ssh_port: int = typer.Option(22),
    key_file: str | None = typer.Option(None),
    proxy_jump: str | None = typer.Option(None),
    http_proxy: str | None = typer.Option(
        None,
        "--http-proxy",
        help="Proxy the node itself uses for outbound HTTP(S), e.g. "
        "http://127.0.0.1:7890. Resolved on the node, not on this machine.",
    ),
    client_id: str = typer.Option("client-109"),
    inventory: Path = typer.Option(
        Path("/etc/frp-manager/inventory.yaml"), "--inventory"
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    target = store(inventory, None)
    inv = target.load()
    node_id = node_id or "node-" + host.replace(".", "-")
    password = (
        None if key_file else getpass.getpass(f"SSH password for {user}@{host}: ")
    )
    sudo_password = getpass.getpass("sudo password (empty if not required): ") or None
    client = inv.client(client_id)
    client_password = (
        None
        if client.ssh.key_file
        else getpass.getpass(f"SSH password for {client.ssh.user}@{client.ssh.host}: ")
    )
    client_sudo = (
        getpass.getpass("client sudo password (empty if not required): ") or None
    )
    node = Node(
        node_id,
        SSHConfig(host, user, ssh_port, key_file, proxy_jump, http_proxy=http_proxy),
        inv.next_tunnel_port(),
    )
    try:
        checks = Manager(target).add_node(
            node,
            client_id,
            Credentials(password, sudo_password),
            Credentials(client_password, client_sudo),
            dry_run=dry_run,
        )
        for check in checks:
            console.print(
                f"[{'green' if check.ok else 'red'}]{check.name}[/]: {check.detail}"
            )
        console.print(
            "Dry-run complete."
            if dry_run
            else f"Node [green]{node_id}[/green] deployed and saved."
        )
    except FrpCtlError as exc:
        fail(exc)


@node_app.command("remove")
def node_remove(
    node_id: str,
    inventory: Path = typer.Option(
        Path("/etc/frp-manager/inventory.yaml"), "--inventory"
    ),
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    if not yes:
        console.print("This removes desired state only; pass --yes to confirm.")
        raise typer.Exit(2)
    try:
        Manager(store(inventory, None)).remove_node(node_id)
        console.print(
            f"Removed {node_id} from desired state; remote services were not deleted."
        )
    except FrpCtlError as exc:
        fail(exc)


@node_app.command("list")
def node_list(
    inventory: Path = typer.Option(
        Path("/etc/frp-manager/inventory.yaml"), "--inventory"
    ),
) -> None:
    inv = store(inventory, None).load()
    table = Table("ID", "Host", "User", "Tunnel", "Version", "Mode", "Proxy")
    for node in inv.nodes:
        table.add_row(
            node.id,
            node.ssh.host,
            node.ssh.user,
            str(node.tunnel_port),
            node.frp_version,
            "legacy" if node.legacy_tunnel else "managed",
            node.ssh.http_proxy or "-",
        )
    console.print(table)


@mapping_app.command("list")
def mapping_list(
    client_id: str = typer.Option("client-109"),
    inventory: Path = typer.Option(
        Path("/etc/frp-manager/inventory.yaml"), "--inventory"
    ),
) -> None:
    client = store(inventory, None).load().client(client_id)
    table = Table("ID", "Protocol", "Local", "Remote")
    for item in client.mappings:
        remote = (
            str(item.remote_port) if item.remote_port else ",".join(item.custom_domains)
        )
        table.add_row(
            item.id, item.protocol, f"{item.local_ip}:{item.local_port}", remote
        )
    console.print(table)


@mapping_app.command("add")
def mapping_add(
    mapping_id: str,
    local_port: int,
    remote_port: int | None = typer.Argument(None),
    protocol: str = typer.Option("tcp"),
    local_ip: str = typer.Option("127.0.0.1"),
    domain: list[str] = typer.Option([], "--domain"),
    client_id: str = typer.Option("client-109"),
    inventory: Path = typer.Option(
        Path("/etc/frp-manager/inventory.yaml"), "--inventory"
    ),
) -> None:
    target = store(inventory, None)
    with target.lock():
        inv = target.load()
        client = inv.client(client_id)
        client.mappings.append(
            Mapping(mapping_id, protocol, local_ip, local_port, remote_port, domain)
        )
        inv.validate()
        target.save(inv)
    console.print(f"Added mapping {mapping_id}; run sync to apply it.")


@mapping_app.command("remove")
def mapping_remove(
    mapping_id: str,
    client_id: str = typer.Option("client-109"),
    inventory: Path = typer.Option(
        Path("/etc/frp-manager/inventory.yaml"), "--inventory"
    ),
) -> None:
    target = store(inventory, None)
    with target.lock():
        inv = target.load()
        client = inv.client(client_id)
        if not any(x.id == mapping_id for x in client.mappings):
            fail(FrpCtlError(f"unknown mapping: {mapping_id}"))
        client.mappings = [x for x in client.mappings if x.id != mapping_id]
        target.save(inv)
    console.print(f"Removed mapping {mapping_id}; run sync to apply it.")


@mapping_app.command("edit")
def mapping_edit(
    mapping_id: str,
    local_ip: str | None = typer.Option(None),
    local_port: int | None = typer.Option(None),
    remote_port: int | None = typer.Option(None),
    client_id: str = typer.Option("client-109"),
    inventory: Path = typer.Option(
        Path("/etc/frp-manager/inventory.yaml"), "--inventory"
    ),
) -> None:
    target = store(inventory, None)
    with target.lock():
        inv = target.load()
        client = inv.client(client_id)
        try:
            item = next(x for x in client.mappings if x.id == mapping_id)
        except StopIteration:
            fail(FrpCtlError(f"unknown mapping: {mapping_id}"))
            return
        if local_ip is not None:
            item.local_ip = local_ip
        if local_port is not None:
            item.local_port = local_port
        if remote_port is not None:
            item.remote_port = remote_port
        inv.validate()
        target.save(inv)
    console.print(f"Updated mapping {mapping_id}; run sync to apply it.")


@proxy_app.command("set")
def proxy_set(
    url: str,
    node_id: str | None = typer.Option(None, "--node"),
    client_id: str | None = typer.Option(None, "--client"),
    inventory: Path = typer.Option(
        Path("/etc/frp-manager/inventory.yaml"), "--inventory"
    ),
) -> None:
    """Record the proxy a host uses for its own outbound HTTP(S) traffic."""
    target = store(inventory, None)
    try:
        with target.lock():
            inv = target.load()
            host_ssh(inv, node_id, client_id).http_proxy = url
            target.save(inv)
    except FrpCtlError as exc:
        fail(exc)
    console.print(f"Proxy for {node_id or client_id} set to [green]{url}[/green]")


@proxy_app.command("clear")
def proxy_clear(
    node_id: str | None = typer.Option(None, "--node"),
    client_id: str | None = typer.Option(None, "--client"),
    inventory: Path = typer.Option(
        Path("/etc/frp-manager/inventory.yaml"), "--inventory"
    ),
) -> None:
    target = store(inventory, None)
    try:
        with target.lock():
            inv = target.load()
            host_ssh(inv, node_id, client_id).http_proxy = None
            target.save(inv)
    except FrpCtlError as exc:
        fail(exc)
    console.print(f"Proxy for {node_id or client_id} cleared")


@proxy_app.command("list")
def proxy_list(
    inventory: Path = typer.Option(
        Path("/etc/frp-manager/inventory.yaml"), "--inventory"
    ),
) -> None:
    inv = store(inventory, None).load()
    table = Table("Kind", "ID", "Target", "Proxy")
    for client in inv.clients:
        table.add_row(
            "client",
            client.id,
            f"{client.ssh.user}@{client.ssh.host}",
            client.ssh.http_proxy or "-",
        )
    for node in inv.nodes:
        table.add_row(
            "node",
            node.id,
            f"{node.ssh.user}@{node.ssh.host}",
            node.ssh.http_proxy or "-",
        )
    console.print(table)
    console.print(
        "Proxy URLs are resolved on the host itself, so 127.0.0.1 means a proxy "
        "running there."
    )


@app.command("plan")
def show_plan(
    node_id: str,
    client_id: str = typer.Option("client-109"),
    inventory: Path = typer.Option(
        Path("/etc/frp-manager/inventory.yaml"), "--inventory"
    ),
) -> None:
    target = store(inventory, None)
    inv, secrets_data = target.load(), target.load_secrets()
    node, client = inv.node(node_id), inv.client(client_id)
    token = (
        secrets_data.get("nodes", {})
        .get(node_id, {})
        .get("token", "<generated-on-sync>")
    )
    console.rule("frps.toml")
    console.print(redact_toml(render_frps(node, client, token)), markup=False)
    console.rule("frpc.toml")
    console.print(redact_toml(render_frpc(node, client, token)), markup=False)


@app.command("doctor")
def doctor(
    inventory: Path = typer.Option(
        Path("/etc/frp-manager/inventory.yaml"), "--inventory"
    ),
) -> None:
    try:
        target = store(inventory, None)
        inv = target.load()
        inv.validate()
        mode = (
            oct(inventory.stat().st_mode & 0o777) if inventory.exists() else "missing"
        )
        console.print(
            f"inventory: [green]valid[/green], clients={len(inv.clients)}, nodes={len(inv.nodes)}, mode={mode}"
        )
        if inventory.exists() and mode != "0o600":
            console.print("[yellow]warning: inventory should be mode 0600[/yellow]")
    except Exception as exc:
        fail(exc)


@app.command("sync")
def sync(
    node_id: str | None = typer.Option(None, "--node"),
    all_nodes: bool = typer.Option(False, "--all"),
    client_id: str = typer.Option("client-109", "--client"),
    inventory: Path = typer.Option(
        Path("/etc/frp-manager/inventory.yaml"), "--inventory"
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    target = store(inventory, None)
    inv = target.load()
    if all_nodes == bool(node_id):
        fail(FrpCtlError("choose exactly one of --node ID or --all"))
    client = inv.client(client_id)
    client_password = (
        None
        if client.ssh.key_file
        else getpass.getpass(f"SSH password for {client.ssh.user}@{client.ssh.host}: ")
    )
    client_sudo = (
        getpass.getpass("client sudo password (empty if not required): ") or None
    )
    selected = (
        [inv.node(node_id)] if node_id else [inv.node(x) for x in client.node_ids]
    )
    for node in selected:
        node_password = (
            None
            if node.ssh.key_file
            else getpass.getpass(f"SSH password for {node.ssh.user}@{node.ssh.host}: ")
        )
        node_sudo = (
            getpass.getpass(f"sudo password for {node.id} (empty if not required): ")
            or None
        )
        try:
            checks = Manager(target).sync_node(
                node.id,
                client_id,
                Credentials(node_password, node_sudo),
                Credentials(client_password, client_sudo),
                dry_run=dry_run,
            )
            for check in checks:
                console.print(
                    f"[{'green' if check.ok else 'red'}]{node.id}/{check.name}[/]: {check.detail}"
                )
            console.print(
                "Dry-run complete."
                if dry_run
                else f"Synchronized {client_id} to {node.id}."
            )
        except FrpCtlError as exc:
            fail(exc)


@app.command("status")
def status(
    inventory: Path = typer.Option(
        Path("/etc/frp-manager/inventory.yaml"), "--inventory"
    ),
) -> None:
    inv = store(inventory, None).load()
    table = Table("Client", "Node", "Endpoint", "Tunnel", "Mappings")
    for client in inv.clients:
        for node_id in client.node_ids:
            node = inv.node(node_id)
            table.add_row(
                client.id,
                node.id,
                node.ssh.host,
                str(node.tunnel_port),
                str(len(client.mappings)),
            )
    console.print(table)
    console.print(
        "Use [bold]frpctl sync --node ID --dry-run[/bold] for live SSH and port checks."
    )


@app.command("tui")
def tui(
    inventory: Path = typer.Option(
        Path("/etc/frp-manager/inventory.yaml"), "--inventory"
    ),
) -> None:
    from .tui import FrpCtlApp

    FrpCtlApp(inventory).run()


def main() -> int:
    try:
        app()
    except FrpCtlError as exc:
        console.print(f"[red]error:[/red] {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
