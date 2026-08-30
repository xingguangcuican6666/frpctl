"""Widgets and helpers shared by the ``frpctl`` and ``frpdomain`` terminal UIs."""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Label, RichLog

from .errors import FrpCtlError
from .models import Client, Inventory

LEVEL_STYLE = {"ok": "green", "warn": "yellow", "error": "red"}


class LogPaneMixin:
    """Writes to a ``RichLog`` mounted with ``id="log"``."""

    def write_log(self, message: str, level: str = "info") -> None:
        """Append one line to the log pane.

        The text is written as a styled ``Text`` rather than markup so that
        remote output and validation messages containing ``[`` are safe.
        """
        widget = self.query_one("#log", RichLog)
        widget.write(Text(message, style=LEVEL_STYLE.get(level, "")))


def row_key(table: DataTable) -> str | None:
    """Row key under the cursor, or ``None`` when nothing is highlighted."""
    if not table.row_count:
        return None
    try:
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
    except Exception:
        return None
    return None if key is None else str(key)


def resolve_client(inventory: Inventory, node_id: str, preferred: str | None) -> Client:
    """Pick the client serving ``node_id``, using ``preferred`` to break ties."""
    clients = [x for x in inventory.clients if node_id in x.node_ids]
    if not clients:
        raise FrpCtlError(f"{node_id} is not attached to any client")
    if len(clients) == 1:
        return clients[0]
    match = next((x for x in clients if x.id == preferred), None)
    if match is None:
        names = ", ".join(x.id for x in clients)
        raise FrpCtlError(
            f"{node_id} serves several clients ({names}); set the client ID "
            "field to choose one"
        )
    return match


class ConfirmScreen(ModalScreen[bool]):
    """Yes/no gate placed in front of every destructive action."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "dismiss_false", "Cancel"),
        Binding("y", "confirm", "Confirm"),
    ]
    CSS = """
    ConfirmScreen { align: center middle; }
    #confirm-box {
        width: 72; height: auto; padding: 1 2;
        border: round $warning; background: $surface;
    }
    #confirm-detail { color: $text-muted; margin-top: 1; }
    #confirm-box Button { margin: 1 1 0 0; }
    """

    def __init__(self, title: str, detail: str, confirm_label: str = "Delete") -> None:
        super().__init__()
        self.heading = title
        self.detail = detail
        self.confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label(self.heading)
            yield Label(self.detail, id="confirm-detail")
            with Horizontal():
                yield Button(self.confirm_label, id="confirm", variant="error")
                yield Button("Cancel", id="cancel", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    @on(Button.Pressed, "#confirm")
    def action_confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def action_dismiss_false(self) -> None:
        self.dismiss(False)
