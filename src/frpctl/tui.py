from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    RichLog,
    TabbedContent,
    TabPane,
)

from .deploy import Check
from .errors import FrpCtlError
from .inventory import InventoryStore
from .models import Mapping, Node, SSHConfig
from .service import Credentials, Manager, cloned_node, resolve_client
from .tui_widgets import ConfirmScreen, LogPaneMixin, row_key

HELP = (
    "↑↓ move  ·  d delete  ·  e edit  ·  c clone  ·  p proxy  ·  s sync  "
    "·  r refresh  ·  q quit"
)


class CredentialsScreen(ModalScreen[tuple[Credentials, Credentials, bool] | None]):
    """Collect the passwords for a single sync run; nothing is persisted."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]
    CSS = """
    CredentialsScreen { align: center middle; }
    #credentials-box {
        width: 78; height: auto; max-height: 90%; padding: 1 2;
        border: round $accent; background: $surface;
    }
    #credentials-box Input { margin-bottom: 1; }
    #credentials-box .field { color: $text-muted; }
    #credentials-box Button { margin-right: 1; }
    """

    def __init__(
        self, node_id: str, node_target: str, client_id: str, client_target: str
    ) -> None:
        super().__init__()
        self.node_id = node_id
        self.node_target = node_target
        self.client_id = client_id
        self.client_target = client_target

    def compose(self) -> ComposeResult:
        with Vertical(id="credentials-box"):
            yield Label(f"Synchronize {self.client_id} -> {self.node_id}")
            yield Label(f"Node SSH password ({self.node_target})", classes="field")
            yield Input(password=True, id="cred-node-password")
            yield Label("Node sudo password (empty if not required)", classes="field")
            yield Input(password=True, id="cred-node-sudo")
            yield Label(f"Client SSH password ({self.client_target})", classes="field")
            yield Input(password=True, id="cred-client-password")
            yield Label("Client sudo password (empty if not required)", classes="field")
            yield Input(password=True, id="cred-client-sudo")
            with Horizontal():
                yield Button("Dry run", id="cred-dry-run", variant="primary")
                yield Button("Sync", id="cred-sync", variant="success")
                yield Button("Cancel", id="cred-cancel")

    def on_mount(self) -> None:
        self.query_one("#cred-node-password", Input).focus()

    def _dismiss_with(self, dry_run: bool) -> None:
        def value(selector: str) -> str | None:
            return self.query_one(selector, Input).value or None

        self.dismiss(
            (
                Credentials(value("#cred-node-password"), value("#cred-node-sudo")),
                Credentials(value("#cred-client-password"), value("#cred-client-sudo")),
                dry_run,
            )
        )

    @on(Button.Pressed, "#cred-dry-run")
    def dry_run(self) -> None:
        self._dismiss_with(True)

    @on(Button.Pressed, "#cred-sync")
    def sync(self) -> None:
        self._dismiss_with(False)

    @on(Button.Pressed, "#cred-cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class ProxyScreen(ModalScreen[str | None]):
    """Edit the outbound proxy one host uses for itself."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]
    CSS = """
    ProxyScreen { align: center middle; }
    #proxy-box {
        width: 78; height: auto; padding: 1 2;
        border: round $accent; background: $surface;
    }
    #proxy-box Input { margin: 1 0; }
    #proxy-box .field { color: $text-muted; }
    #proxy-box Button { margin-right: 1; }
    """

    def __init__(self, label: str, current: str | None) -> None:
        super().__init__()
        self.label = label
        self.current = current or ""

    def compose(self) -> ComposeResult:
        with Vertical(id="proxy-box"):
            yield Label(f"Outbound proxy for {self.label}")
            yield Label(
                "Resolved on that host, so 127.0.0.1 means a proxy running there. "
                "Leave empty to use no proxy.",
                classes="field",
            )
            yield Input(
                value=self.current, placeholder="http://127.0.0.1:7890", id="proxy-url"
            )
            with Horizontal():
                yield Button("Save", id="proxy-save", variant="success")
                yield Button("Cancel", id="proxy-cancel")

    def on_mount(self) -> None:
        self.query_one("#proxy-url", Input).focus()

    @on(Input.Submitted, "#proxy-url")
    @on(Button.Pressed, "#proxy-save")
    def save(self) -> None:
        self.dismiss(self.query_one("#proxy-url", Input).value.strip())

    @on(Button.Pressed, "#proxy-cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class NodeEditScreen(ModalScreen[dict[str, str] | None]):
    """Edit an existing node's connection info; nothing is deployed here."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]
    CSS = """
    NodeEditScreen { align: center middle; }
    #node-edit-box {
        width: 78; height: auto; max-height: 90%; padding: 1 2;
        border: round $accent; background: $surface;
    }
    #node-edit-box Input { margin-bottom: 1; }
    #node-edit-box .field { color: $text-muted; }
    #node-edit-box Button { margin-right: 1; }
    """

    # (attribute, label, placeholder); host/user/port/version fall back to the
    # current value when blank, the nullable fields below are cleared by a blank.
    FIELDS: ClassVar[tuple[tuple[str, str, str], ...]] = (
        ("host", "Public host", "198.51.100.20"),
        ("user", "SSH user", "ubuntu"),
        ("port", "SSH port", "22"),
        ("key_file", "SSH key file (blank clears it)", "~/.ssh/id_ed25519"),
        ("proxy_jump", "SSH jump host (blank clears it)", "bastion"),
        (
            "http_proxy",
            "Outbound proxy on the node (blank clears it)",
            "http://127.0.0.1:7890",
        ),
        (
            "ssh_proxy",
            "SSH SOCKS proxy on this machine (blank clears it)",
            "socks5h://127.0.0.1:1080",
        ),
        ("frp_version", "FRP version installed on the next sync", "0.70.0"),
    )

    def __init__(self, node: Node) -> None:
        super().__init__()
        self.node = node
        self.current = {
            "host": node.ssh.host,
            "user": node.ssh.user,
            "port": str(node.ssh.port),
            "key_file": node.ssh.key_file or "",
            "proxy_jump": node.ssh.proxy_jump or "",
            "http_proxy": node.ssh.http_proxy or "",
            "ssh_proxy": node.ssh.ssh_proxy or "",
            "frp_version": node.frp_version,
        }

    @staticmethod
    def selector(key: str) -> str:
        return f"#edit-{key.replace('_', '-')}"

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="node-edit-box"):
            yield Label(f"Edit node {self.node.id}")
            for key, label, placeholder in self.FIELDS:
                yield Label(label, classes="field")
                yield Input(
                    value=self.current[key],
                    placeholder=placeholder,
                    id=self.selector(key)[1:],
                    type="integer" if key == "port" else "text",
                )
            with Horizontal():
                yield Button("Save", id="node-edit-save", variant="success")
                yield Button("Cancel", id="node-edit-cancel")

    def on_mount(self) -> None:
        self.query_one("#edit-host", Input).focus()

    @on(Button.Pressed, "#node-edit-save")
    def save(self) -> None:
        self.dismiss(
            {
                key: self.query_one(self.selector(key), Input).value.strip()
                for key, _, _ in self.FIELDS
            }
        )

    @on(Button.Pressed, "#node-edit-cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class FrpCtlApp(LogPaneMixin, App):
    TITLE = "FRP Multi-node Manager"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("r", "reload", "Refresh"),
        Binding("d", "delete_selected", "Delete"),
        Binding("e", "edit_selected", "Edit"),
        Binding("c", "clone_selected", "Clone"),
        Binding("p", "proxy_selected", "Proxy"),
        Binding("s", "sync_selected", "Sync"),
        Binding("q", "quit", "Quit"),
    ]
    CSS = """
    #help { color: $text-muted; padding: 0 1; }
    #nodes, #mappings { height: 1fr; border: round $primary; }
    #nodes:focus, #mappings:focus { border: round $accent; }
    #log { height: 9; border: round $secondary; padding: 0 1; }
    #node-form, #mapping-form { padding: 1 2; }
    #node-form Input, #mapping-form Input { margin-bottom: 1; }
    #node-source { color: $accent; margin-bottom: 1; }
    .field { color: $text-muted; }
    .actions { height: auto; }
    .actions Button { margin-right: 1; }
    """

    def __init__(self, inventory_path: Path):
        super().__init__()
        self.store = InventoryStore(
            inventory_path, inventory_path.with_name("secrets.yaml")
        )
        self.busy = False
        # Node id whose configuration the Add node tab will copy, set by "c".
        self.clone_from: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(id="tabs"):
            with TabPane("Overview", id="overview"):
                with Vertical():
                    yield Label(HELP, id="help")
                    yield DataTable(id="nodes")
                    yield DataTable(id="mappings")
            with TabPane("Add node", id="add-node"):
                yield from self.node_form()
            with TabPane("Mapping", id="mapping"):
                yield from self.mapping_form()
        yield RichLog(id="log", wrap=True)
        yield Footer()

    def node_form(self) -> ComposeResult:
        with VerticalScroll(id="node-form"):
            yield Label("Deploying a new node", id="node-source")
            yield Label("Node ID (blank derives one from the host)", classes="field")
            yield Input(placeholder="node-198-51-100-20", id="node-id")
            yield Label("Public host", classes="field")
            yield Input(placeholder="198.51.100.20", id="node-host")
            yield Label("SSH user", classes="field")
            yield Input(placeholder="ubuntu", id="node-user")
            yield Label("SSH port", classes="field")
            yield Input(value="22", id="node-port", type="integer")
            yield Label("Outbound proxy on the node (blank for none)", classes="field")
            yield Input(placeholder="http://127.0.0.1:7890", id="node-proxy")
            yield Label(
                "SSH SOCKS proxy on this machine (blank for none)", classes="field"
            )
            yield Input(placeholder="socks5h://127.0.0.1:1080", id="node-ssh-proxy")
            yield Label("SSH password (blank uses key auth)", classes="field")
            yield Input(password=True, id="node-password")
            yield Label("sudo password (empty if not required)", classes="field")
            yield Input(password=True, id="node-sudo")
            yield Label("Client ID to mirror onto this node", classes="field")
            yield Input(value="client-109", id="client-id")
            yield Label("Client SSH password", classes="field")
            yield Input(password=True, id="client-password")
            yield Label("Client sudo password (empty if not required)", classes="field")
            yield Input(password=True, id="client-sudo")
            with Horizontal(classes="actions"):
                yield Button("Preflight", id="preflight", variant="primary")
                yield Button("Deploy", id="deploy", variant="success")
                yield Button("Reset", id="node-reset")

    def mapping_form(self) -> ComposeResult:
        with VerticalScroll(id="mapping-form"):
            yield Label("Client ID", classes="field")
            yield Input(value="client-109", id="map-client-id")
            yield Label("Mapping ID", classes="field")
            yield Input(placeholder="web", id="map-id")
            yield Label("Protocol: tcp, udp, http, https", classes="field")
            yield Input(value="tcp", id="map-protocol")
            yield Label("Local IP", classes="field")
            yield Input(value="127.0.0.1", id="map-local-ip")
            yield Label("Local port", classes="field")
            yield Input(placeholder="3000", id="map-local-port", type="integer")
            yield Label("Remote port (TCP/UDP only)", classes="field")
            yield Input(placeholder="13000", id="map-remote-port", type="integer")
            yield Label("Domains, comma separated (HTTP/HTTPS only)", classes="field")
            yield Input(placeholder="example.com, api.example.com", id="map-domains")
            with Horizontal(classes="actions"):
                yield Button("Add", id="add-mapping", variant="success")
                yield Button("Save changes", id="save-mapping", variant="primary")
                yield Button("Clear", id="clear-mapping")

    def on_mount(self) -> None:
        nodes = self.query_one("#nodes", DataTable)
        nodes.add_columns(
            "ID", "Host", "User", "Tunnel", "Mode", "Clients", "Proxy", "SSH via"
        )
        mappings = self.query_one("#mappings", DataTable)
        mappings.add_columns("Client", "ID", "Protocol", "Local", "Remote")
        for table in (nodes, mappings):
            table.cursor_type = "row"
            table.zebra_stripes = True
        self.query_one("#log", RichLog).border_title = "Log"
        self.refresh_tables()
        nodes.focus()
        self.write_log(f"Inventory: {self.store.inventory_path}")

    def refresh_tables(self) -> None:
        try:
            inventory = self.store.load()
        except Exception as exc:
            self.write_log(f"ERROR {exc}", "error")
            return
        nodes = self.query_one("#nodes", DataTable)
        mappings = self.query_one("#mappings", DataTable)
        cursors = (nodes.cursor_row, mappings.cursor_row)
        nodes.clear()
        mappings.clear()
        for node in inventory.nodes:
            attached = [x.id for x in inventory.clients if node.id in x.node_ids]
            nodes.add_row(
                node.id,
                node.ssh.host,
                node.ssh.user,
                str(node.tunnel_port),
                "legacy" if node.legacy_tunnel else "managed",
                ", ".join(attached) or "-",
                node.ssh.http_proxy or "-",
                node.ssh.ssh_proxy or "-",
                key=node.id,
            )
        for client in inventory.clients:
            for item in client.mappings:
                remote = (
                    str(item.remote_port)
                    if item.remote_port
                    else ", ".join(item.custom_domains)
                )
                mappings.add_row(
                    client.id,
                    item.id,
                    item.protocol,
                    f"{item.local_ip}:{item.local_port}",
                    remote or "-",
                    key=f"{client.id}/{item.id}",
                )
        nodes.border_title = f"Nodes ({nodes.row_count})"
        mappings.border_title = f"Mappings ({mappings.row_count})"
        for table, row in zip((nodes, mappings), cursors, strict=True):
            if table.row_count:
                table.move_cursor(row=min(row, table.row_count - 1))

    def selection(self, expected: str | None = None) -> tuple[DataTable, str] | None:
        """Return the focused overview table and its highlighted row key."""
        table = self.focused if isinstance(self.focused, DataTable) else None
        if table is None or (expected is not None and table.id != expected):
            wanted = expected or "Nodes or Mappings"
            self.write_log(f"Focus the {wanted} table and highlight a row", "warn")
            return None
        key = row_key(table)
        if key is None:
            self.write_log(f"No row is highlighted in {table.id}", "warn")
            return None
        return table, key

    def action_reload(self) -> None:
        self.refresh_tables()
        self.write_log("Reloaded desired state")

    def action_delete_selected(self) -> None:
        found = self.selection()
        if found is None:
            return
        table, key = found
        if table.id == "nodes":
            self.push_screen(
                ConfirmScreen(
                    f"Delete node {key}?",
                    "Removes it from the desired state and forgets its token. "
                    "frps on the node and frpc/tunnel units on the client keep "
                    "running; remove them there manually.",
                    "Delete node",
                ),
                lambda ok: self.delete_node(key) if ok else None,
            )
            return
        client_id, _, mapping_id = key.partition("/")
        self.push_screen(
            ConfirmScreen(
                f"Delete mapping {mapping_id}?",
                f"Removes it from client {client_id}. Every node keeps serving it "
                "until you sync.",
                "Delete mapping",
            ),
            lambda ok: self.delete_mapping(client_id, mapping_id) if ok else None,
        )

    def delete_node(self, node_id: str) -> None:
        try:
            Manager(self.store).remove_node(node_id)
        except Exception as exc:
            self.write_log(f"ERROR {exc}", "error")
            return
        self.write_log(f"Deleted node {node_id} from the desired state", "ok")
        self.refresh_tables()

    def delete_mapping(self, client_id: str, mapping_id: str) -> None:
        try:
            with self.store.lock():
                inventory = self.store.load()
                client = inventory.client(client_id)
                if not any(x.id == mapping_id for x in client.mappings):
                    raise FrpCtlError(f"unknown mapping: {mapping_id}")
                client.mappings = [x for x in client.mappings if x.id != mapping_id]
                self.store.save(inventory)
        except Exception as exc:
            self.write_log(f"ERROR {exc}", "error")
            return
        self.write_log(
            f"Deleted mapping {client_id}/{mapping_id}; sync nodes to apply it", "ok"
        )
        self.refresh_tables()

    def action_proxy_selected(self) -> None:
        found = self.selection()
        if found is None:
            return
        table, key = found
        if table.id == "nodes":
            kind, host_id = "node", key
        else:
            kind, host_id = "client", key.partition("/")[0]
        try:
            inventory = self.store.load()
            ssh = (
                inventory.node(host_id).ssh
                if kind == "node"
                else inventory.client(host_id).ssh
            )
        except Exception as exc:
            self.write_log(f"ERROR {exc}", "error")
            return
        self.push_screen(
            ProxyScreen(f"{kind} {host_id} ({ssh.user}@{ssh.host})", ssh.http_proxy),
            lambda url: self.save_proxy(kind, host_id, url),
        )

    def save_proxy(self, kind: str, host_id: str, url: str | None) -> None:
        if url is None:
            self.write_log("Proxy unchanged")
            return
        try:
            with self.store.lock():
                inventory = self.store.load()
                ssh = (
                    inventory.node(host_id).ssh
                    if kind == "node"
                    else inventory.client(host_id).ssh
                )
                ssh.http_proxy = url or None
                self.store.save(inventory)
        except Exception as exc:
            self.write_log(f"ERROR {exc}", "error")
            return
        detail = f"set to {url}" if url else "cleared"
        self.write_log(f"Proxy for {kind} {host_id} {detail}", "ok")
        self.refresh_tables()

    def action_clone_selected(self) -> None:
        found = self.selection("nodes")
        if found is None:
            return
        source_id = found[1]
        try:
            inventory = self.store.load()
            source = inventory.node(source_id)
            client = resolve_client(
                inventory, source_id, self.query_one("#map-client-id", Input).value
            )
        except Exception as exc:
            self.write_log(f"ERROR {exc}", "error")
            return
        fields = {
            "#node-id": "",
            "#node-host": "",
            "#node-user": source.ssh.user,
            "#node-port": str(source.ssh.port),
            "#node-proxy": source.ssh.http_proxy or "",
            "#node-ssh-proxy": source.ssh.ssh_proxy or "",
            "#client-id": client.id,
        }
        for selector, value in fields.items():
            self.query_one(selector, Input).value = value
        self.clone_from = source_id
        self.query_one("#node-source", Label).update(
            f"Cloning {source_id}: frp {source.frp_version}, bind "
            f"{source.frps_bind_port}, vhost {source.vhost_http_port}/"
            f"{source.vhost_https_port}, overrides {source.port_overrides or '{}'}"
        )
        self.query_one("#tabs", TabbedContent).active = "add-node"
        self.query_one("#node-host", Input).focus()
        self.write_log(
            f"Cloning {source_id}; enter the new host and passwords, then Deploy"
        )
        if source.port_overrides:
            self.write_log(
                f"The twin inherits port overrides {source.port_overrides}; use "
                "frpdomain clone and apply to put Nginx in front of them",
                "warn",
            )

    @on(Button.Pressed, "#node-reset")
    def reset_node_form(self) -> None:
        for selector in (
            "#node-id",
            "#node-host",
            "#node-user",
            "#node-proxy",
            "#node-ssh-proxy",
        ):
            self.query_one(selector, Input).value = ""
        self.query_one("#node-port", Input).value = "22"
        self.clone_from = None
        self.query_one("#node-source", Label).update("Deploying a new node")
        self.write_log("Node form reset; nothing will be inherited")

    def action_edit_selected(self) -> None:
        found = self.selection()
        if found is None:
            return
        table, key = found
        if table.id == "nodes":
            self.edit_node(key)
            return
        client_id, _, mapping_id = key.partition("/")
        self.load_mapping_form(client_id, mapping_id)

    def edit_node(self, node_id: str) -> None:
        try:
            node = self.store.load().node(node_id)
        except Exception as exc:
            self.write_log(f"ERROR {exc}", "error")
            return
        self.push_screen(
            NodeEditScreen(node),
            lambda values: self.save_node_edit(node_id, values),
        )

    def save_node_edit(self, node_id: str, values: dict[str, str] | None) -> None:
        if values is None:
            self.write_log("Node unchanged")
            return
        try:
            with self.store.lock():
                inventory = self.store.load()
                node = inventory.node(node_id)
                ssh = node.ssh
                ssh.host = values["host"] or ssh.host
                ssh.user = values["user"] or ssh.user
                ssh.port = int(values["port"]) if values["port"] else ssh.port
                ssh.key_file = values["key_file"] or None
                ssh.proxy_jump = values["proxy_jump"] or None
                ssh.http_proxy = values["http_proxy"] or None
                ssh.ssh_proxy = values["ssh_proxy"] or None
                node.frp_version = values["frp_version"] or node.frp_version
                inventory.validate()
                self.store.save(inventory)
        except Exception as exc:
            self.write_log(f"ERROR {exc}", "error")
            return
        self.write_log(f"Updated node {node_id}; sync it to apply the change", "ok")
        self.refresh_tables()

    @on(DataTable.RowSelected, "#mappings")
    def edit_row(self, event: DataTable.RowSelected) -> None:
        client_id, _, mapping_id = str(event.row_key.value).partition("/")
        self.load_mapping_form(client_id, mapping_id)

    def load_mapping_form(self, client_id: str, mapping_id: str) -> None:
        try:
            client = self.store.load().client(client_id)
            item = next(
                (x for x in client.mappings if x.id == mapping_id),
                None,
            )
            if item is None:
                raise FrpCtlError(f"unknown mapping: {client_id}/{mapping_id}")
        except Exception as exc:
            self.write_log(f"ERROR {exc}", "error")
            return
        fields = {
            "#map-client-id": client_id,
            "#map-id": item.id,
            "#map-protocol": item.protocol,
            "#map-local-ip": item.local_ip,
            "#map-local-port": str(item.local_port),
            "#map-remote-port": ""
            if item.remote_port is None
            else str(item.remote_port),
            "#map-domains": ", ".join(item.custom_domains),
        }
        for selector, value in fields.items():
            self.query_one(selector, Input).value = value
        self.query_one("#tabs", TabbedContent).active = "mapping"
        self.query_one("#map-local-port", Input).focus()
        self.write_log(f"Editing {client_id}/{mapping_id}; press Save changes to store")

    def mapping_from_form(self) -> Mapping:
        def value(selector: str) -> str:
            return self.query_one(selector, Input).value.strip()

        local_port = value("#map-local-port")
        if not local_port:
            raise FrpCtlError("local port is required")
        remote_port = value("#map-remote-port")
        return Mapping(
            value("#map-id"),
            value("#map-protocol").lower() or "tcp",
            value("#map-local-ip") or "127.0.0.1",
            int(local_port),
            int(remote_port) if remote_port else None,
            [x.strip() for x in value("#map-domains").split(",") if x.strip()],
        )

    @on(Button.Pressed, "#add-mapping")
    def add_mapping(self) -> None:
        self.write_mapping(update=False)

    @on(Button.Pressed, "#save-mapping")
    def save_mapping(self) -> None:
        self.write_mapping(update=True)

    @on(Button.Pressed, "#clear-mapping")
    def clear_mapping(self) -> None:
        for selector in (
            "#map-id",
            "#map-local-port",
            "#map-remote-port",
            "#map-domains",
        ):
            self.query_one(selector, Input).value = ""
        self.query_one("#map-protocol", Input).value = "tcp"
        self.query_one("#map-local-ip", Input).value = "127.0.0.1"
        self.write_log("Mapping form cleared")

    def write_mapping(self, *, update: bool) -> None:
        try:
            mapping = self.mapping_from_form()
            with self.store.lock():
                inventory = self.store.load()
                client = inventory.client(self.query_one("#map-client-id", Input).value)
                existing = next(
                    (x for x in client.mappings if x.id == mapping.id), None
                )
                if update:
                    if existing is None:
                        raise FrpCtlError(f"unknown mapping: {mapping.id}")
                    # The form cannot express locations/extra, so preserve whatever
                    # an imported frpc.toml contributed.
                    mapping.locations = existing.locations
                    mapping.extra = existing.extra
                    client.mappings[client.mappings.index(existing)] = mapping
                elif existing is not None:
                    raise FrpCtlError(f"mapping already exists: {mapping.id}")
                else:
                    client.mappings.append(mapping)
                self.store.save(inventory)
        except Exception as exc:
            self.write_log(f"ERROR {exc}", "error")
            return
        action = "Updated" if update else "Added"
        self.write_log(f"{action} mapping {mapping.id}; sync nodes to apply it", "ok")
        self.refresh_tables()

    def action_sync_selected(self) -> None:
        if self.busy:
            self.write_log("A remote operation is already running", "warn")
            return
        found = self.selection("nodes")
        if found is None:
            return
        node_id = found[1]
        try:
            inventory = self.store.load()
            node = inventory.node(node_id)
            client = resolve_client(
                inventory, node_id, self.query_one("#map-client-id", Input).value
            )
        except Exception as exc:
            self.write_log(f"ERROR {exc}", "error")
            return
        self.push_screen(
            CredentialsScreen(
                node.id,
                f"{node.ssh.user}@{node.ssh.host}",
                client.id,
                f"{client.ssh.user}@{client.ssh.host}",
            ),
            lambda result: self.start_sync(node.id, client.id, result),
        )

    def start_sync(
        self,
        node_id: str,
        client_id: str,
        result: tuple[Credentials, Credentials, bool] | None,
    ) -> None:
        if result is None:
            self.write_log("Sync cancelled")
            return
        node_credentials, client_credentials, dry_run = result
        self.busy = True
        self.sync_worker(
            node_id, client_id, node_credentials, client_credentials, dry_run
        )

    def report_checks(self, prefix: str, checks: list[Check]) -> None:
        for check in checks:
            self.call_from_thread(
                self.write_log,
                f"{prefix}/{check.name}: {check.detail}",
                "ok" if check.ok else "error",
            )

    @work(thread=True)
    def sync_worker(
        self,
        node_id: str,
        client_id: str,
        node_credentials: Credentials,
        client_credentials: Credentials,
        dry_run: bool,
    ) -> None:
        label = "Dry-run" if dry_run else "Sync"
        try:
            self.call_from_thread(
                self.write_log, f"{label} {client_id} -> {node_id} started"
            )
            checks = Manager(self.store).sync_node(
                node_id,
                client_id,
                node_credentials,
                client_credentials,
                dry_run=dry_run,
            )
            self.report_checks(node_id, checks)
            self.call_from_thread(
                self.write_log,
                f"{label} complete"
                if dry_run
                else f"Synchronized {client_id} to {node_id}",
                "ok",
            )
            if not dry_run:
                self.call_from_thread(self.refresh_tables)
        except Exception as exc:
            self.call_from_thread(self.write_log, f"ERROR {exc}", "error")
        finally:
            self.busy = False

    @on(Button.Pressed, "#preflight")
    def preflight(self) -> None:
        self.start_deploy(dry_run=True)

    @on(Button.Pressed, "#deploy")
    def deploy(self) -> None:
        self.start_deploy(dry_run=False)

    def start_deploy(self, *, dry_run: bool) -> None:
        if self.busy:
            self.write_log("A remote operation is already running", "warn")
            return

        def value(selector: str) -> str:
            return self.query_one(selector, Input).value.strip()

        host = value("#node-host")
        if not host:
            self.write_log("Public host is required", "warn")
            return
        # Read the form on the message pump; the worker thread must not touch widgets.
        form = {
            "node_id": value("#node-id") or "node-" + host.replace(".", "-"),
            "host": host,
            "user": value("#node-user"),
            "port": value("#node-port") or "22",
            "proxy": value("#node-proxy"),
            "ssh_proxy": value("#node-ssh-proxy"),
            "client_id": value("#client-id"),
            "clone_from": self.clone_from or "",
        }
        node_credentials = Credentials(
            value("#node-password") or None, value("#node-sudo") or None
        )
        client_credentials = Credentials(
            value("#client-password") or None, value("#client-sudo") or None
        )
        if dry_run:
            self.launch_deploy(form, node_credentials, client_credentials, True)
            return
        self.push_screen(
            ConfirmScreen(
                f"Deploy frps to {host}?",
                "Installs frps on the node plus tunnel and frpc units on the "
                "client, then saves the node only if every check passes.",
                "Deploy",
            ),
            lambda ok: (
                self.launch_deploy(form, node_credentials, client_credentials, False)
                if ok
                else None
            ),
        )

    def launch_deploy(
        self,
        form: dict[str, str],
        node_credentials: Credentials,
        client_credentials: Credentials,
        dry_run: bool,
    ) -> None:
        self.busy = True
        self.deploy_worker(form, node_credentials, client_credentials, dry_run)

    @work(thread=True)
    def deploy_worker(
        self,
        form: dict[str, str],
        node_credentials: Credentials,
        client_credentials: Credentials,
        dry_run: bool,
    ) -> None:
        label = "Preflight" if dry_run else "Deployment"
        try:
            inventory = self.store.load()
            ssh = SSHConfig(
                form["host"],
                form["user"],
                int(form["port"]),
                http_proxy=form["proxy"] or None,
                ssh_proxy=form["ssh_proxy"] or None,
            )
            tunnel_port = inventory.next_tunnel_port()
            if form["clone_from"]:
                source = inventory.node(form["clone_from"])
                node = cloned_node(source, form["node_id"], ssh, tunnel_port)
                label += f" of the {form['clone_from']} clone"
            else:
                node = Node(form["node_id"], ssh, tunnel_port)
            self.call_from_thread(self.write_log, f"{label} of {node.id} started")
            checks = Manager(self.store).add_node(
                node,
                form["client_id"],
                node_credentials,
                client_credentials,
                dry_run=dry_run,
            )
            self.report_checks(node.id, checks)
            self.call_from_thread(self.write_log, f"{label} succeeded", "ok")
            if not dry_run:
                self.call_from_thread(self.refresh_tables)
                self.call_from_thread(self.reset_node_form)
        except Exception as exc:
            self.call_from_thread(self.write_log, f"ERROR {exc}", "error")
        finally:
            self.busy = False
