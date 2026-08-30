from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

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
from .tui_widgets import ConfirmScreen, LogPaneMixin, resolve_client, row_key

HELP = "↑↓ move  ·  d delete  ·  e edit  ·  g plan  ·  a apply  ·  r refresh  ·  q quit"


@dataclass(slots=True)
class ApplyRequest:
    """One apply run: an address for Let's Encrypt plus per-host credentials."""

    email: str
    node: Credentials
    client: Credentials
    skip_dns_check: bool
    edge_proxy: bool


class ApplyScreen(ModalScreen[ApplyRequest | None]):
    """Collect everything one ``frpdomain apply`` run needs; nothing is stored."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]
    CSS = """
    ApplyScreen { align: center middle; }
    #apply-box {
        width: 84; height: auto; max-height: 95%; padding: 1 2;
        border: round $warning; background: $surface;
    }
    #apply-box Input { margin-bottom: 1; }
    #apply-box .field { color: $text-muted; }
    #apply-box .warning { color: $warning; margin-bottom: 1; }
    #apply-box Button { margin-right: 1; }
    """

    def __init__(
        self,
        node_id: str,
        node_target: str,
        client_id: str,
        client_target: str,
        hostnames: list[str],
        email: str | None,
    ) -> None:
        super().__init__()
        self.node_id = node_id
        self.node_target = node_target
        self.client_id = client_id
        self.client_target = client_target
        self.hostnames = hostnames
        self.email = email or ""

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="apply-box"):
            yield Label(f"Apply domains on {self.node_id} using {self.client_id}")
            yield Label(
                "Installs Nginx and Certbot, moves FRP off port 80 on this node "
                "only, then issues certificates for: " + ", ".join(self.hostnames),
                classes="warning",
            )
            yield Label("Certificate contact email", classes="field")
            yield Input(value=self.email, placeholder="admin@example.com", id="a-email")
            yield Label(f"Node SSH password ({self.node_target})", classes="field")
            yield Input(password=True, id="a-node-password")
            yield Label("Node sudo password (empty if not required)", classes="field")
            yield Input(password=True, id="a-node-sudo")
            yield Label(f"Client SSH password ({self.client_target})", classes="field")
            yield Input(password=True, id="a-client-password")
            yield Label("Client sudo password (empty if not required)", classes="field")
            yield Input(password=True, id="a-client-sudo")
            yield Checkbox("Skip the DNS A-record check", id="a-skip-dns")
            yield Checkbox(
                "Behind a CDN/ESA edge, so DNS points away from the origin",
                id="a-edge-proxy",
            )
            with Horizontal():
                yield Button("Apply", id="a-apply", variant="error")
                yield Button("Cancel", id="a-cancel", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#a-email", Input).focus()

    @on(Button.Pressed, "#a-apply")
    def apply(self) -> None:
        def value(selector: str) -> str | None:
            return self.query_one(selector, Input).value or None

        email = (self.query_one("#a-email", Input).value or "").strip()
        if not email:
            self.notify("A certificate contact email is required", severity="warning")
            return
        self.dismiss(
            ApplyRequest(
                email,
                Credentials(value("#a-node-password"), value("#a-node-sudo")),
                Credentials(value("#a-client-password"), value("#a-client-sudo")),
                self.query_one("#a-skip-dns", Checkbox).value,
                self.query_one("#a-edge-proxy", Checkbox).value,
            )
        )

    @on(Button.Pressed, "#a-cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class FrpDomainApp(LogPaneMixin, App):
    TITLE = "FRP Domain & HTTPS Manager"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("r", "reload", "Refresh"),
        Binding("d", "delete_selected", "Delete"),
        Binding("e", "edit_selected", "Edit"),
        Binding("g", "plan_selected", "Plan"),
        Binding("a", "apply_selected", "Apply"),
        Binding("q", "quit", "Quit"),
    ]
    CSS = """
    #help { color: $text-muted; padding: 0 1; }
    #table, #dns-table { height: 1fr; border: round $primary; }
    #table:focus, #dns-table:focus { border: round $accent; }
    #log { height: 9; border: round $secondary; padding: 0 1; }
    #binding-form, #plan-box { padding: 1 2; }
    #binding-form Input { margin-bottom: 1; }
    #plan-box { border: round $primary; }
    .field { color: $text-muted; }
    .actions { height: auto; }
    .actions Button { margin-right: 1; }
    """

    def __init__(self, inventory_path: Path, domains_path: Path):
        super().__init__()
        self.store = InventoryStore(
            inventory_path, inventory_path.with_name("secrets.yaml")
        )
        self.domains = DomainStore(domains_path)
        self.busy = False
        # (node_id, hostname) currently loaded into the form, so that changing
        # either field renames the binding instead of leaving a duplicate.
        self.editing: tuple[str, str] | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(id="tabs"):
            with TabPane("Bindings", id="bindings"):
                with Vertical():
                    yield Label(HELP, id="help")
                    yield DataTable(id="table")
            with TabPane("Binding", id="binding"):
                yield from self.binding_form()
            with TabPane("DNS", id="dns"):
                yield DataTable(id="dns-table")
            with TabPane("Nginx plan", id="plan"):
                with VerticalScroll(id="plan-box"):
                    yield Static(
                        "Highlight a binding and press g to render the final "
                        "Nginx configuration for its node.",
                        id="plan-text",
                    )
        yield RichLog(id="log", wrap=True)
        yield Footer()

    def binding_form(self) -> ComposeResult:
        with VerticalScroll(id="binding-form"):
            yield Label("Node ID that will serve this hostname", classes="field")
            yield Input(placeholder="node-198-51-100-20", id="b-node")
            yield Label("Hostname", classes="field")
            yield Input(placeholder="api.example.com", id="b-hostname")
            yield Label("Upstream FRP port on that node", classes="field")
            yield Input(placeholder="13000", id="b-port", type="integer")
            yield Label(
                "Client ID (only used to break ties when a node serves several)",
                classes="field",
            )
            yield Input(value="client-109", id="b-client")
            with Horizontal(classes="actions"):
                yield Button("Save", id="b-save", variant="success")
                yield Button("Clear", id="b-clear")

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.add_columns("Node", "Hostname", "Upstream", "Cert email", "Node IP")
        dns = self.query_one("#dns-table", DataTable)
        dns.add_columns("Type", "Name", "Value")
        for widget in (table, dns):
            widget.cursor_type = "row"
            widget.zebra_stripes = True
        self.query_one("#log", RichLog).border_title = "Log"
        self.refresh_tables()
        table.focus()
        self.write_log(f"Domains: {self.domains.path}")
        self.write_log(f"Inventory: {self.store.inventory_path}")

    def node_addresses(self) -> dict[str, str]:
        """Map node id to public host, tolerating an unreadable frpctl inventory."""
        try:
            return {x.id: x.ssh.host for x in self.store.load().nodes}
        except Exception as exc:
            self.write_log(f"ERROR reading the frpctl inventory: {exc}", "error")
            return {}

    def refresh_tables(self) -> None:
        try:
            desired = self.domains.load()
        except Exception as exc:
            self.write_log(f"ERROR {exc}", "error")
            return
        addresses = self.node_addresses()
        table = self.query_one("#table", DataTable)
        dns = self.query_one("#dns-table", DataTable)
        cursors = (table.cursor_row, dns.cursor_row)
        table.clear()
        dns.clear()
        for node in desired.nodes:
            address = addresses.get(node.node_id, "unknown node")
            for binding in node.bindings:
                table.add_row(
                    node.node_id,
                    binding.hostname,
                    str(binding.upstream_port),
                    node.email or "-",
                    address,
                    key=f"{node.node_id}/{binding.hostname}",
                )
                dns.add_row("A", binding.hostname, address)
        table.border_title = f"Bindings ({table.row_count})"
        dns.border_title = "Records to create manually at the DNS provider"
        for widget, row in zip((table, dns), cursors, strict=True):
            if widget.row_count:
                widget.move_cursor(row=min(row, widget.row_count - 1))

    def action_reload(self) -> None:
        self.refresh_tables()
        self.write_log("Reloaded desired state")

    def selected_binding(self) -> tuple[str, str] | None:
        """Node id and hostname of the highlighted row in the Bindings table.

        The DNS table is read-only, so every action works on the Bindings table
        regardless of which one currently has focus.
        """
        key = row_key(self.query_one("#table", DataTable))
        if key is None:
            self.write_log("No binding is highlighted", "warn")
            return None
        node_id, _, hostname = key.partition("/")
        return node_id, hostname

    def action_delete_selected(self) -> None:
        found = self.selected_binding()
        if found is None:
            return
        node_id, hostname = found
        self.push_screen(
            ConfirmScreen(
                f"Delete binding {hostname}?",
                f"Removes it from the desired state of {node_id}. The Nginx server "
                "block and the certificate stay on the node until you apply again.",
                "Delete binding",
            ),
            lambda ok: self.delete_binding(node_id, hostname) if ok else None,
        )

    def delete_binding(self, node_id: str, hostname: str) -> None:
        try:
            desired = self.domains.load()
            node = desired.node(node_id)
            node.bindings = [x for x in node.bindings if x.hostname != hostname]
            self.domains.save(desired)
        except Exception as exc:
            self.write_log(f"ERROR {exc}", "error")
            return
        if self.editing == (node_id, hostname):
            self.editing = None
        self.write_log(
            f"Deleted {hostname} from {node_id}; apply to update Nginx", "ok"
        )
        self.refresh_tables()

    def action_edit_selected(self) -> None:
        found = self.selected_binding()
        if found is None:
            return
        self.load_binding_form(*found)

    @on(DataTable.RowSelected, "#table")
    def edit_row(self, event: DataTable.RowSelected) -> None:
        node_id, _, hostname = str(event.row_key.value).partition("/")
        self.load_binding_form(node_id, hostname)

    def load_binding_form(self, node_id: str, hostname: str) -> None:
        try:
            node = self.domains.load().node(node_id)
            binding = next((x for x in node.bindings if x.hostname == hostname), None)
            if binding is None:
                raise FrpCtlError(f"unknown binding: {node_id}/{hostname}")
        except Exception as exc:
            self.write_log(f"ERROR {exc}", "error")
            return
        self.query_one("#b-node", Input).value = node_id
        self.query_one("#b-hostname", Input).value = binding.hostname
        self.query_one("#b-port", Input).value = str(binding.upstream_port)
        self.editing = (node_id, hostname)
        self.query_one("#tabs", TabbedContent).active = "binding"
        self.query_one("#b-port", Input).focus()
        self.write_log(f"Editing {node_id}/{hostname}; press Save to store it")

    @on(Button.Pressed, "#b-clear")
    def clear_binding(self) -> None:
        for selector in ("#b-node", "#b-hostname", "#b-port"):
            self.query_one(selector, Input).value = ""
        self.editing = None
        self.write_log("Binding form cleared")

    @on(Button.Pressed, "#b-save")
    def save_binding(self) -> None:
        def value(selector: str) -> str:
            return self.query_one(selector, Input).value.strip()

        node_id = value("#b-node")
        hostname = value("#b-hostname").lower().rstrip(".")
        try:
            port_text = value("#b-port")
            if not port_text:
                raise FrpCtlError("upstream port is required")
            # Refuse hostnames for nodes frpctl does not manage; apply would fail
            # later with a much less obvious message.
            self.store.load().node(node_id)
            desired = self.domains.load()
            self.drop_renamed_binding(desired, node_id, hostname)
            node = desired.node(node_id, create=True)
            existing = next((x for x in node.bindings if x.hostname == hostname), None)
            if existing is not None:
                existing.upstream_port = int(port_text)
            else:
                node.bindings.append(DomainBinding(hostname, int(port_text)))
            self.domains.save(desired)
        except Exception as exc:
            self.write_log(f"ERROR {exc}", "error")
            return
        self.editing = (node_id, hostname)
        self.write_log(
            f"Saved {hostname} -> {node_id}:{port_text}; apply to update Nginx", "ok"
        )
        self.refresh_tables()

    def drop_renamed_binding(self, desired, node_id: str, hostname: str) -> None:
        """Remove the row that was loaded into the form when it has been renamed."""
        if self.editing is None or self.editing == (node_id, hostname):
            return
        old_node_id, old_hostname = self.editing
        try:
            previous = desired.node(old_node_id)
        except FrpCtlError:
            return
        previous.bindings = [x for x in previous.bindings if x.hostname != old_hostname]
        self.write_log(f"Renamed {old_node_id}/{old_hostname} -> {node_id}/{hostname}")

    def action_plan_selected(self) -> None:
        found = self.selected_binding()
        if found is None:
            return
        node_id = found[0]
        try:
            # A deep copy: relocate_listener_port mutates port_overrides, and a
            # preview must not touch the inventory on disk.
            preview = copy.deepcopy(self.store.load())
            node = preview.node(node_id)
            client = resolve_client(
                preview, node_id, self.query_one("#b-client", Input).value
            )
            before = dict(node.port_overrides)
            relocated = relocate_listener_port(preview, node, client)
            config = self.domains.load().node(node_id)
            rendered = render_nginx(node, client, config, tls=True)
        except Exception as exc:
            self.write_log(f"ERROR {exc}", "error")
            return
        self.query_one("#plan-text", Static).update(Text(rendered))
        self.query_one("#tabs", TabbedContent).active = "plan"
        if node.port_overrides != before:
            self.write_log(
                f"FRP port 80 will move to {relocated} on {node_id} only", "warn"
            )
        self.write_log(f"Rendered the Nginx plan for {node_id} using {client.id}")

    def action_apply_selected(self) -> None:
        if self.busy:
            self.write_log("A remote operation is already running", "warn")
            return
        found = self.selected_binding()
        if found is None:
            return
        node_id = found[0]
        try:
            inventory = self.store.load()
            node = inventory.node(node_id)
            client = resolve_client(
                inventory, node_id, self.query_one("#b-client", Input).value
            )
            config = self.domains.load().node(node_id)
            hostnames = [x.hostname for x in config.bindings]
            if not hostnames:
                raise FrpCtlError(f"{node_id} has no domain bindings")
        except Exception as exc:
            self.write_log(f"ERROR {exc}", "error")
            return
        self.push_screen(
            ApplyScreen(
                node_id,
                f"{node.ssh.user}@{node.ssh.host}",
                client.id,
                f"{client.ssh.user}@{client.ssh.host}",
                hostnames,
                config.email,
            ),
            lambda request: self.start_apply(node_id, client.id, request),
        )

    def start_apply(
        self, node_id: str, client_id: str, request: ApplyRequest | None
    ) -> None:
        if request is None:
            self.write_log("Apply cancelled")
            return
        self.busy = True
        self.apply_worker(node_id, client_id, request)

    @work(thread=True)
    def apply_worker(self, node_id: str, client_id: str, request: ApplyRequest) -> None:
        try:
            self.call_from_thread(
                self.write_log,
                f"Apply for {node_id} started; installing Nginx and issuing "
                "certificates usually takes a few minutes",
            )
            DomainManager(self.store, self.domains).apply(
                node_id,
                client_id,
                request.email,
                request.node,
                request.client,
                skip_dns_check=request.skip_dns_check,
                edge_proxy=request.edge_proxy,
            )
            self.call_from_thread(
                self.write_log,
                f"Configured HTTP/HTTPS bindings for {node_id}",
                "ok",
            )
        except Exception as exc:
            self.call_from_thread(self.write_log, f"ERROR {exc}", "error")
        finally:
            self.busy = False
            # apply() rewrites both the domain file and the frpctl inventory when
            # it relocates port 80, and restores them on failure.
            self.call_from_thread(self.refresh_tables)
