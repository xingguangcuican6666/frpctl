"""Widgets and helpers shared by the ``frpctl`` and ``frpdomain`` terminal UIs."""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, RichLog

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


class PromptScreen(ModalScreen[str | None]):
    """Ask for one line of text; returns the stripped value, or None on cancel."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]
    CSS = """
    PromptScreen { align: center middle; }
    #prompt-box {
        width: 78; height: auto; padding: 1 2;
        border: round $accent; background: $surface;
    }
    #prompt-box Input { margin: 1 0; }
    #prompt-detail { color: $text-muted; }
    #prompt-box Button { margin-right: 1; }
    """

    def __init__(
        self,
        title: str,
        detail: str,
        placeholder: str = "",
        value: str = "",
        confirm_label: str = "Save",
    ) -> None:
        super().__init__()
        self.heading = title
        self.detail = detail
        self.placeholder = placeholder
        self.value = value
        self.confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-box"):
            yield Label(self.heading)
            yield Label(self.detail, id="prompt-detail")
            yield Input(
                value=self.value, placeholder=self.placeholder, id="prompt-value"
            )
            with Horizontal():
                yield Button(self.confirm_label, id="prompt-ok", variant="success")
                yield Button("Cancel", id="prompt-cancel")

    def on_mount(self) -> None:
        self.query_one("#prompt-value", Input).focus()

    @on(Input.Submitted, "#prompt-value")
    @on(Button.Pressed, "#prompt-ok")
    def confirm(self) -> None:
        self.dismiss(self.query_one("#prompt-value", Input).value.strip())

    @on(Button.Pressed, "#prompt-cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)
