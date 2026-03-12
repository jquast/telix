"""
Macro key binding support for the REPL client.

Provides :class:`Macro` for representing key-to-text bindings and
:func:`build_macro_dispatch` for building a blessed key name to handler
mapping.

Keys are stored as blessed key names (e.g. ``KEY_F1``, ``KEY_ALT_E``)
or single characters, matching :attr:`blessed.keyboard.Keystroke.name`
and ``str(keystroke)`` respectively.
"""

# std imports
import typing
import asyncio
import logging
import datetime
import dataclasses
from typing import TYPE_CHECKING

import blessed.line_editor

from . import util

if TYPE_CHECKING:
    from .session_context import TelixSessionContext

__all__ = (
    "BUILTIN_MACROS",
    "Macro",
    "build_macro_dispatch",
    "ensure_builtin_macros",
    "key_name_to_ansi_seq",
    "key_name_to_seq",
    "load_macros",
    "save_macros",
)


@dataclasses.dataclass
class Macro:
    """
    A single key-to-text macro binding.

    :param key: Blessed key name (e.g. ``KEY_F5``, ``KEY_ALT_E``).
    :param text: Text to insert/send, with ``;`` as command separators.
    :param builtin: When true, the macro is a system default that cannot
        be deleted in the editor.
    :param builtin_name: Stable identifier for builtin macros (e.g.
        ``"help"``, ``"edit_macros"``).  Empty for user-defined macros.
    """

    key: str
    text: str
    enabled: bool = True
    last_used: str = ""
    toggle: bool = False
    toggle_text: str = ""
    toggle_state: bool = False
    builtin: bool = False
    builtin_name: str = ""


def parse_entries(entries: list[dict[str, str]]) -> list[Macro]:
    """Parse a list of macro entry dicts into :class:`Macro` instances."""
    macros: list[Macro] = []
    for entry in entries:
        key = entry.get("key", "").strip()
        text = entry.get("text", "")
        if not key:
            continue
        enabled = bool(entry.get("enabled", True))
        last_used = str(entry.get("last_used", ""))
        toggle = bool(entry.get("toggle", False))
        toggle_text = str(entry.get("toggle_text", ""))
        builtin = bool(entry.get("builtin", False))
        builtin_name = str(entry.get("builtin_name", ""))
        macros.append(
            Macro(
                key=key,
                text=text,
                enabled=enabled,
                last_used=last_used,
                toggle=toggle,
                toggle_text=toggle_text,
                builtin=builtin,
                builtin_name=builtin_name,
            )
        )
    return macros


def load_macros(path: str, session_key: str) -> list[Macro]:
    """
    Load macro definitions for a session from a JSON file.

    The file is keyed by session (``"host:port"``).  Each value is
    an object with a ``"macros"`` list.

    :param path: Path to the macros JSON file.
    :param session_key: Session identifier (``"host:port"``).
    :returns: List of :class:`Macro` instances.
    :raises FileNotFoundError: When *path* does not exist.
    :raises ValueError: When JSON structure is invalid.
    """
    entries = util.load_json_entries(path, session_key, "macros")
    return parse_entries(entries)


def save_macros(path: str, macros: list[Macro], session_key: str) -> None:
    """
    Save macro definitions for a session to a JSON file.

    Other sessions' data in the file is preserved.

    :param path: Path to the macros JSON file.
    :param macros: List of :class:`Macro` instances to save.
    :param session_key: Session identifier (``"host:port"``).
    """
    entries = [
        {
            "key": m.key,
            "text": m.text,
            **({"enabled": False} if not m.enabled else {}),
            **({"last_used": m.last_used} if m.last_used else {}),
            **({"toggle": True, "toggle_text": m.toggle_text} if m.toggle else {}),
            **({"builtin": True, "builtin_name": m.builtin_name} if m.builtin else {}),
        }
        for m in macros
    ]
    util.save_json_entries(path, session_key, "macros", entries)


# Blessed key name to VT100/xterm ANSI escape sequence, for ansi_keys mode.
_SPECIAL_KEY_MAP: dict[str, str] = {
    "KEY_UP": "\x1b[A",
    "KEY_DOWN": "\x1b[B",
    "KEY_RIGHT": "\x1b[C",
    "KEY_LEFT": "\x1b[D",
    "KEY_HOME": "\x1b[H",
    "KEY_END": "\x1b[F",
    "KEY_PGUP": "\x1b[5~",
    "KEY_PGDOWN": "\x1b[6~",
    "KEY_INSERT": "\x1b[2~",
    "KEY_DELETE": "\x1b[3~",
    "KEY_BTAB": "\x1b[Z",
    "KEY_F1": "\x1bOP",
    "KEY_F2": "\x1bOQ",
    "KEY_F3": "\x1bOR",
    "KEY_F4": "\x1bOS",
    "KEY_F5": "\x1b[15~",
    "KEY_F6": "\x1b[17~",
    "KEY_F7": "\x1b[18~",
    "KEY_F8": "\x1b[19~",
    "KEY_F9": "\x1b[20~",
    "KEY_F10": "\x1b[21~",
    "KEY_F11": "\x1b[23~",
    "KEY_F12": "\x1b[24~",
}


def key_name_to_ansi_seq(name: str) -> str | None:
    """
    Return the ANSI escape sequence for a blessed key name, or ``None``.

    Used when ``ansi_keys`` is enabled to transmit raw escape sequences for
    navigation keys that blessed normally absorbs and returns as named
    :class:`~blessed.keyboard.Keystroke` objects.

    :param name: Blessed key name (e.g. ``KEY_UP``, ``KEY_F1``).
    :returns: ANSI escape sequence string, or ``None`` if not mapped.
    """
    return _SPECIAL_KEY_MAP.get(name)


# Ctrl key name to ASCII control character offset.
_CTRL_CHAR_MAP: dict[str, str] = {
    "KEY_CTRL_A": "\x01",
    "KEY_CTRL_B": "\x02",
    "KEY_CTRL_C": "\x03",
    "KEY_CTRL_D": "\x04",
    "KEY_CTRL_E": "\x05",
    "KEY_CTRL_F": "\x06",
    "KEY_CTRL_G": "\x07",
    "KEY_CTRL_H": "\x08",
    "KEY_CTRL_I": "\x09",
    "KEY_CTRL_J": "\x0a",
    "KEY_CTRL_K": "\x0b",
    "KEY_CTRL_L": "\x0c",
    "KEY_CTRL_M": "\x0d",
    "KEY_CTRL_N": "\x0e",
    "KEY_CTRL_O": "\x0f",
    "KEY_CTRL_P": "\x10",
    "KEY_CTRL_Q": "\x11",
    "KEY_CTRL_R": "\x12",
    "KEY_CTRL_S": "\x13",
    "KEY_CTRL_T": "\x14",
    "KEY_CTRL_U": "\x15",
    "KEY_CTRL_V": "\x16",
    "KEY_CTRL_W": "\x17",
    "KEY_CTRL_X": "\x18",
    "KEY_CTRL_Y": "\x19",
    "KEY_CTRL_Z": "\x1a",
    "KEY_CTRL_OPEN_BRACKET": "\x1b",
    "KEY_CTRL_BACKSLASH": "\x1c",
    "KEY_CTRL_CLOSE_BRACKET": "\x1d",
    "KEY_CTRL_CARET": "\x1e",
    "KEY_CTRL_UNDERSCORE": "\x1f",
}


def key_name_to_seq(name: str) -> str | None:
    """
    Convert a blessed key name to a raw character sequence.

    Returns ``None`` for key names that have no single-sequence
    representation (e.g. F-keys, which are multi-byte terminal
    escape sequences handled by blessed's keyboard database).

    :param name: Blessed key name (e.g. ``KEY_CTRL_L``, ``KEY_ALT_H``).
    :returns: Raw character sequence, or ``None``.
    """
    if name in _CTRL_CHAR_MAP:
        return _CTRL_CHAR_MAP[name]
    if name.startswith("KEY_ALT_SHIFT_"):
        letter = name[len("KEY_ALT_SHIFT_") :]
        if len(letter) == 1 and letter.isalpha():
            return "\x1b" + letter.upper()
    if name.startswith("KEY_ALT_"):
        letter = name[len("KEY_ALT_") :]
        if len(letter) == 1 and letter.isalpha():
            return "\x1b" + letter.lower()
    return None


BUILTIN_MACROS: list[Macro] = [
    Macro(key="KEY_F1", text="`help`", builtin=True, builtin_name="help"),
    Macro(key="KEY_F3", text="`randomwalk dialog`", builtin=True, builtin_name="randomwalk_dialog"),
    Macro(key="KEY_F4", text="`autodiscover dialog`", builtin=True, builtin_name="autodiscover_dialog"),
    Macro(key="KEY_F5", text="`resume walk`", builtin=True, builtin_name="resume_walk"),
    Macro(key="KEY_ALT_H", text="`edit highlights`", builtin=True, builtin_name="edit_highlights"),
    Macro(key="KEY_ALT_M", text="`edit macros`", builtin=True, builtin_name="edit_macros"),
    Macro(key="KEY_ALT_T", text="`edit triggers`", builtin=True, builtin_name="edit_triggers"),
    Macro(key="KEY_ALT_R", text="`edit rooms`", builtin=True, builtin_name="edit_rooms"),
    Macro(key="KEY_ALT_C", text="`captures`", builtin=True, builtin_name="edit_captures"),
    Macro(key="KEY_ALT_B", text="`edit bars`", builtin=True, builtin_name="edit_bars"),
    Macro(key="KEY_ALT_E", text="`edit theme`", builtin=True, builtin_name="edit_theme"),
    Macro(key="KEY_ALT_SHIFT_H", text="`toggle highlights`", builtin=True, builtin_name="toggle_highlights"),
    Macro(key="KEY_ALT_SHIFT_T", text="`toggle triggers`", builtin=True, builtin_name="toggle_triggers"),
    Macro(key="KEY_ALT_Q", text="`stopscript`", builtin=True, builtin_name="stopscript"),
    Macro(key="KEY_CTRL_L", text="`repaint`", builtin=True, builtin_name="repaint"),
    Macro(key="KEY_CTRL_CLOSE_BRACKET", text="`disconnect`", builtin=True, builtin_name="disconnect"),
]


def ensure_builtin_macros(macros: list[Macro]) -> list[Macro]:
    """
    Ensure all builtin macros are present in a macro list.

    Missing builtins are appended.  Existing builtins (matched by
    ``builtin_name``) are preserved, keeping any user key overrides.

    :param macros: Existing macro list (may be empty).
    :returns: New list with all builtins guaranteed present.
    """
    existing_names = {m.builtin_name for m in macros if m.builtin_name}
    result = list(macros)
    for builtin in BUILTIN_MACROS:
        if builtin.builtin_name not in existing_names:
            result.append(dataclasses.replace(builtin))
    return result


def build_macro_dispatch(macros: list[Macro], ctx: "TelixSessionContext", log: logging.Logger) -> dict[str, typing.Any]:
    """
    Build a blessed key name to handler mapping from macro defs.

    Keys are matched directly against
    :attr:`~blessed.keyboard.Keystroke.name` (for named keys like
    ``KEY_F1``) or ``str(keystroke)`` (for single chars).  Macros bound
    to keys in :data:`blessed.line_editor.DEFAULT_KEYMAP` are skipped
    with a warning.

    :param macros: Macro definitions to bind.
    :param ctx: :class:`~telix.session_context.SessionContext` instance.
    :param log: Logger instance.
    :returns: Dict mapping blessed key names (or raw chars) to handlers.
    """
    from . import client_repl as repl  # circular

    result: dict[str, typing.Any] = {}
    for macro in macros:
        if not macro.enabled:
            continue
        if macro.key in blessed.line_editor.DEFAULT_KEYMAP:
            log.warning("macro %r conflicts with editor keymap, skipping", macro.key)
            continue
        macro_ref = macro

        async def handler(m: Macro = macro_ref) -> None:
            if m.toggle:
                text = m.toggle_text if m.toggle_state else m.text
                m.toggle_state = not m.toggle_state
            else:
                text = m.text
            m.last_used = datetime.datetime.now(datetime.timezone.utc).isoformat()
            if hasattr(ctx, "mark_macros_dirty"):
                ctx.mark_macros_dirty()
            task = asyncio.ensure_future(repl.execute_macro_commands(text, ctx, log))

            def on_done(t: asyncio.Task[None]) -> None:
                if not t.cancelled() and t.exception() is not None:
                    log.warning("macro execution failed: %s", t.exception())

            task.add_done_callback(on_done)

        seq = key_name_to_seq(macro.key)
        result[seq if seq is not None else macro.key] = handler
    return result
