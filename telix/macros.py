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
import os
import json
import typing
import asyncio
import logging
import datetime
import dataclasses

import blessed.line_editor

from . import paths

__all__ = ("Macro", "build_macro_dispatch", "load_macros", "save_macros")


@dataclasses.dataclass
class Macro:
    """
    A single key-to-text macro binding.

    :param key: Blessed key name (e.g. ``KEY_F5``, ``KEY_ALT_E``).
    :param text: Text to insert/send, with ``;`` as command separators.
    """

    key: str
    text: str
    enabled: bool = True
    last_used: str = ""
    toggle: bool = False
    toggle_text: str = ""
    toggle_state: bool = False


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
        macros.append(
            Macro(key=key, text=text, enabled=enabled, last_used=last_used, toggle=toggle, toggle_text=toggle_text)
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
    with open(path, encoding="utf-8") as fh:
        data: dict[str, typing.Any] = json.load(fh)

    session_data: dict[str, typing.Any] = data.get(session_key, {})
    entries: list[dict[str, str]] = session_data.get("macros", [])
    return parse_entries(entries)


def save_macros(path: str, macros: list[Macro], session_key: str) -> None:
    """
    Save macro definitions for a session to a JSON file.

    Other sessions' data in the file is preserved.

    :param path: Path to the macros JSON file.
    :param macros: List of :class:`Macro` instances to save.
    :param session_key: Session identifier (``"host:port"``).
    """
    data: dict[str, typing.Any] = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)

    data[session_key] = {
        "macros": [
            {
                "key": m.key,
                "text": m.text,
                **({"enabled": False} if not m.enabled else {}),
                **({"last_used": m.last_used} if m.last_used else {}),
                **({"toggle": True, "toggle_text": m.toggle_text} if m.toggle else {}),
            }
            for m in macros
        ]
    }

    content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    paths.atomic_write(path, content)


def build_macro_dispatch(macros: list[Macro], ctx: typing.Any, log: logging.Logger) -> dict[str, typing.Any]:
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
    from . import client_repl as repl  # noqa: PLC0415  # circular

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

        result[macro.key] = handler
    return result
