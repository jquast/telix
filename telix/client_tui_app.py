"""Main Textual application and entry point for the telix TUI."""

# std imports
import sys

# 3rd party
import textual.app
import textual.events

# local
import telix.rooms
from . import client_tui_base


class TelnetSessionApp(textual.app.App[None]):
    """Textual TUI for managing telix client sessions."""

    TITLE = "telix Session Manager"

    def on_mouse_down(self, event: textual.events.MouseDown) -> None:
        """Paste X11 primary selection on middle-click."""
        if event.button != 2:
            return
        event.stop()
        text = client_tui_base.read_primary_selection()
        if not text:
            return
        focused = self.focused
        if focused is not None and hasattr(focused, "insert_text_at_cursor"):
            focused.insert_text_at_cursor(text)

    def set_pointer_shape(self, shape: str) -> None:
        """Disable pointer shape changes to prevent WriterThread deadlock."""

    def on_mount(self) -> None:
        """Push the session list screen on startup."""
        prefs = telix.rooms.load_prefs(client_tui_base.DEFAULTS_KEY)
        saved_theme = prefs.get("tui_theme")
        if isinstance(saved_theme, str) and saved_theme:
            self.theme = saved_theme
        else:
            self.theme = "gruvbox"
        self.push_screen(client_tui_base.SessionListScreen())

    def watch_theme(self, old: str, new: str) -> None:
        """Persist theme choice to global preferences."""
        if new:
            prefs = telix.rooms.load_prefs(client_tui_base.DEFAULTS_KEY)
            prefs["tui_theme"] = new
            telix.rooms.save_prefs(client_tui_base.DEFAULTS_KEY, prefs)


def tui_main() -> None:
    """Launch the Textual TUI session manager."""
    client_tui_base.patch_writer_thread_queue()
    client_tui_base.restore_blocking_fds()
    client_tui_base.terminal.restore_opost()
    sys.stdout.write(client_tui_base.TERMINAL_CLEANUP)
    sys.stdout.flush()
    client_tui_base.terminal.flush_stdin()
    TelnetSessionApp().run()
    sys.stdout.write("\x1b[999;999H\n" + client_tui_base.TERMINAL_CLEANUP + "\n")
    sys.stdout.flush()
