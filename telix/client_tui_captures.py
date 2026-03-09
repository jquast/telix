"""Highlight captures and chat viewer screens for the telix TUI."""

# std imports
import os
import json
import typing

# 3rd party
import wcwidth
import rich.text
import textual.app
import textual.events
import textual.screen
import textual.binding
import textual.widgets
import textual.containers

# local
from . import client_tui_base


class CapsPane(textual.containers.Vertical):
    """Pane widget for captures and chats viewing."""

    BINDINGS: typing.ClassVar[list[textual.binding.Binding]] = [  # type: ignore[assignment]
        textual.binding.Binding("escape", "close", "Close", show=True),
        textual.binding.Binding("f10", "close", "Close", show=False),
        textual.binding.Binding("q", "close", "Close", show=False),
        textual.binding.Binding("f1", "toggle_keys", "Keys", show=True),
        textual.binding.Binding("up", "prev_channel", "Prev", show=False),
        textual.binding.Binding("down", "next_channel", "Next", show=False),
        textual.binding.Binding("tab", "next_channel", "Next Channel", show=True),
        textual.binding.Binding("shift+tab", "prev_channel", "Prev Channel", show=True, priority=True),
    ]

    DEFAULT_CSS = """
    CapsPane {
        width: 100%; height: 100%;
    }
    #chat-sidebar {
        width: 16;
        height: 100%;
        background: $surface;
    }
    #chat-sidebar Button {
        width: 100%;
        height: 1;
        margin: 0;
        padding: 0 1;
        border: none;
        background: $surface-lighten-1;
        color: $text-muted;
    }
    #chat-sidebar Button.active-channel {
        background: $accent;
        color: $text;
        text-style: bold;
    }
    #chat-log {
        height: 100%;
        width: 1fr;
        background: $surface;
        padding: 0 1;
    }
    """

    def __init__(
        self, chat_file: str, session_key: str = "", initial_channel: str = "", capture_file: str = ""
    ) -> None:
        """
        Initialize with path to chat history file.

        :param chat_file: Path to the chat JSON file.
        :param session_key: Session identifier.
        :param initial_channel: Channel to select on open (most recent activity).
        :param capture_file: Path to a JSON file with capture data.
        """
        super().__init__()
        self.chat_file = chat_file
        self.session_key = session_key
        self.initial_channel = initial_channel
        self.capture_file = capture_file
        self.messages: list[dict[str, typing.Any]] = []
        self.channels: list[str] = []
        self.filter_idx: int = 0
        self.captures: dict[str, int] = {}
        self.capture_log: dict[str, list[dict[str, typing.Any]]] = {}

    def compose(self) -> textual.app.ComposeResult:
        """Build the chat viewer layout with a vertical channel sidebar."""
        with textual.containers.Horizontal():
            with textual.containers.Vertical(id="chat-sidebar"):
                pass
            yield textual.widgets.RichLog(highlight=False, markup=False, wrap=True, id="chat-log")

    def on_mount(self) -> None:
        """Load chat file, select initial channel, and populate the log."""
        self.load_messages()
        if self.initial_channel and self.initial_channel in self.channels:
            self.filter_idx = self.channels.index(self.initial_channel) + 1
        self.rebuild_sidebar()
        self.populate_log()
        self.call_after_refresh(self.focus_active_channel)

    def focus_active_channel(self) -> None:
        """Focus the active channel button in the sidebar."""
        buttons = list(self.query("#chat-sidebar Button"))
        if buttons:
            idx = min(self.filter_idx, len(buttons) - 1)
            buttons[idx].focus()

    def focus_default(self) -> None:
        """Focus the active channel button."""
        self.focus_active_channel()

    def load_messages(self) -> None:
        """Read messages from chat JSON and capture data files."""
        if self.chat_file and os.path.exists(self.chat_file):
            with open(self.chat_file, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                self.messages = data
        if self.capture_file and os.path.exists(self.capture_file):
            with open(self.capture_file, encoding="utf-8") as fh:
                cap_data = json.load(fh)
            if isinstance(cap_data, dict):
                self.captures = cap_data.get("captures", {})
                self.capture_log = cap_data.get("capture_log", {})
        channels: set[str] = set()
        for msg in self.messages:
            ch = msg.get("channel", "")
            if ch:
                channels.add(ch)
        for ch in self.capture_log:
            channels.add(ch)
        self.channels = sorted(channels)

    def channel_labels(self) -> list[str]:
        """Return ``["all", ...channels]`` list used for cycling."""
        return ["all"] + self.channels

    def active_filter(self) -> str:
        """Return the current channel filter string, or ``""`` for all."""
        labels = self.channel_labels()
        if self.filter_idx == 0 or self.filter_idx >= len(labels):
            return ""
        return labels[self.filter_idx]

    def rebuild_sidebar(self) -> None:
        """Rebuild the channel sidebar buttons to match current channel list."""
        sidebar = self.query_one("#chat-sidebar", textual.containers.Vertical)
        sidebar.remove_children()
        for i, name in enumerate(self.channel_labels()):
            display = name if len(name) <= 14 else name[:14] + "\u2026"
            btn = textual.widgets.Button(display, id=f"ch-btn-{i}")
            if i == self.filter_idx:
                btn.add_class("active-channel")
            sidebar.mount(btn)

    def update_channel_sidebar(self) -> None:
        """Update active-channel class on sidebar buttons without rebuilding."""
        for btn in self.query("#chat-sidebar Button"):
            btn_idx = int((btn.id or "ch-btn-0").split("-")[-1])
            if btn_idx == self.filter_idx:
                btn.add_class("active-channel")
            else:
                btn.remove_class("active-channel")

    def populate_log(self, channel_filter: str = "") -> None:
        """Fill the RichLog with chat and capture messages, optionally filtered."""
        if not channel_filter:
            channel_filter = self.active_filter()
        log_widget: textual.widgets.RichLog = self.query_one("#chat-log", textual.widgets.RichLog)
        log_widget.clear()

        if channel_filter == "captures" and self.captures:
            header = rich.text.Text("Current Captures:", style="bold underline")
            log_widget.write(header)
            for k, v in sorted(self.captures.items()):
                log_widget.write(rich.text.Text(f"  {k}: {v}"))
            log_widget.write(rich.text.Text(""))

        all_entries: list[tuple[str, str, dict[str, typing.Any]]] = []
        for msg in self.messages:
            ch = msg.get("channel", "")
            if channel_filter and ch != channel_filter:
                continue
            all_entries.append((msg.get("ts", ""), "chat", msg))
        for ch, entries in self.capture_log.items():
            if channel_filter and ch != channel_filter:
                continue
            for entry in entries:
                all_entries.append((entry.get("ts", ""), "capture", {**entry, "channel": ch}))
        all_entries.sort(key=lambda e: e[0])

        log_width = (log_widget.size.width or 80) - 2

        for ts_val, source, msg in all_entries:
            ch = msg.get("channel", "")

            prefix = rich.text.Text()
            prefix_plain = ""

            if ts_val:
                rel = f"{client_tui_base.relative_time(ts_val)} "
                prefix.append(rel, style="dim")
                prefix_plain += rel
            if not channel_filter:
                channel_ansi = msg.get("channel_ansi", "")
                if channel_ansi:
                    ch_rich = rich.text.Text.from_ansi(channel_ansi.rstrip())
                    prefix.append_text(ch_rich)
                    prefix.append(" ")
                    prefix_plain += ch_rich.plain + " "
                else:
                    ch_label = f"[{ch}] "
                    prefix.append(ch_label, style="bold cyan")
                    prefix_plain += ch_label

            if source == "chat":
                talker = msg.get("talker", "")
                body_text = msg.get("text", "").rstrip("\n")
                if talker:
                    for pfx_str in (f"{talker} : ", f"{talker}: ", f"{talker} "):
                        if body_text.startswith(pfx_str):
                            body_text = body_text[len(pfx_str) :]
                            break
                    talker_label = f"{talker}: "
                    prefix.append(talker_label, style="bold")
                    prefix_plain += talker_label
                hl_style = ""
            else:
                body_text = msg.get("line", "")
                hl_style = msg.get("highlight", "")

            indent_width = wcwidth.wcswidth(prefix_plain)
            if indent_width < 0:
                indent_width = len(prefix_plain)
            indent = " " * indent_width
            body_width = max(log_width - indent_width, 10)

            wrapped = wcwidth.wrap(body_text, width=body_width, subsequent_indent="", propagate_sgr=True)
            if not wrapped:
                wrapped = [""]

            for i, wline in enumerate(wrapped):
                out = rich.text.Text()
                out.append_text(prefix) if i == 0 else out.append(indent)
                if hl_style:
                    out.append(wline, style=hl_style.replace("_", " "))
                else:
                    out.append_text(rich.text.Text.from_ansi(wline))
                log_widget.write(out)
        log_widget.scroll_end(animate=False)

    def on_resize(self, event: textual.events.Resize) -> None:
        """Re-wrap messages when the pane is resized."""
        self.populate_log()

    def action_close(self) -> None:
        """Dismiss the chat viewer."""
        self.app.exit()

    def action_toggle_keys(self) -> None:
        """Toggle the Textual keys help panel."""
        if self.app.help_panel:  # type: ignore[attr-defined]
            self.app.action_hide_help_panel()
        else:
            self.app.action_show_help_panel()

    def on_button_pressed(self, event: textual.widgets.Button.Pressed) -> None:
        """Handle channel sidebar button clicks."""
        btn_id = event.button.id or ""
        if btn_id.startswith("ch-btn-"):
            self.filter_idx = int(btn_id.split("-")[-1])
            self.update_channel_sidebar()
            self.populate_log()

    def action_next_channel(self) -> None:
        """Cycle forward through channel filters."""
        if not self.channels:
            return
        labels = self.channel_labels()
        self.filter_idx = (self.filter_idx + 1) % len(labels)
        self.update_channel_sidebar()
        self.populate_log()

    def action_prev_channel(self) -> None:
        """Cycle backward through channel filters."""
        if not self.channels:
            return
        labels = self.channel_labels()
        self.filter_idx = (self.filter_idx - 1) % len(labels)
        self.update_channel_sidebar()
        self.populate_log()


class CapsScreen(textual.screen.Screen[None]):
    """Thin screen wrapper for the captures and chats viewer."""

    def __init__(
        self, chat_file: str, session_key: str = "", initial_channel: str = "", capture_file: str = ""
    ) -> None:
        super().__init__()
        self.pane = CapsPane(
            chat_file=chat_file, session_key=session_key, initial_channel=initial_channel, capture_file=capture_file
        )

    def compose(self) -> textual.app.ComposeResult:
        yield self.pane
        yield textual.widgets.Footer()


ChatViewerScreen = CapsScreen


def run_chat_viewer(chat_file: str, session_key: str = "", initial_channel: str = "", capture_file: str = "") -> None:
    """
    Launch the Capture Window TUI in the current (worker) thread.

    :param chat_file: Path to the chat JSON file.
    :param session_key: Session key for the chat viewer.
    :param initial_channel: Channel to show on open.
    :param capture_file: Path to the captures JSON temp file.
    """
    client_tui_base.log_child_diagnostics()
    client_tui_base.patch_writer_thread_queue()
    app = client_tui_base.EditorApp(
        ChatViewerScreen(  # type: ignore[arg-type]
            chat_file=chat_file, session_key=session_key, initial_channel=initial_channel, capture_file=capture_file
        ),
        session_key=session_key,
    )
    app.run(mouse=False)


def chat_viewer_main(
    chat_file: str, session_key: str = "", initial_channel: str = "", logfile: str = "", capture_file: str = ""
) -> None:
    """Launch standalone Capture Window TUI."""
    client_tui_base.restore_blocking_fds(logfile)
    client_tui_base.log_child_diagnostics()
    client_tui_base.patch_writer_thread_queue()
    app = client_tui_base.EditorApp(
        ChatViewerScreen(  # type: ignore[arg-type]
            chat_file=chat_file, session_key=session_key, initial_channel=initial_channel, capture_file=capture_file
        ),
        session_key=session_key,
    )
    app.run(mouse=False)
