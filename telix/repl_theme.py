"""Resolve the user's Textual theme into concrete hex colors for the blessed REPL."""

import textual.theme

from . import rooms, client_tui_base

DEFAULT_THEME = "gruvbox"

# Mapping of semantic REPL color name -> Textual theme token (key in generate() dict).
TOKEN_MAP: dict[str, str] = {
    "foreground": "foreground",
    "background": "background",
    "surface": "surface",
    "primary": "primary",
    "secondary": "secondary",
    "accent": "accent",
    "success": "success",
    "error": "error",
    "warning": "warning",
    "muted": "secondary-darken-2",
    "input_text": "foreground",
    "input_bg": "surface-darken-3",
    "input_suggestion": "surface-lighten-1",
    "input_ar_text": "warning",
    "input_ar_bg": "warning-darken-3",
    "input_ar_suggestion": "warning-darken-2",
    "dmz_active": "warning",
    "dmz_inactive": "surface-darken-2",
    "cursor_color": "foreground",
    "cursor_ar_color": "warning-lighten-1",
    "active_cmd": "primary-darken-1",
    "bar_text_fill": "background",
    "bar_text_empty": "secondary",
    "bar_empty_bg": "surface",
    "pending_cmd": "secondary-darken-1",
}

# Hardcoded fallback palette when theme resolution fails entirely.
FALLBACK: dict[str, str] = {
    "foreground": "#dddddd",
    "background": "#000000",
    "surface": "#2a2a2a",
    "primary": "#ffff00",
    "secondary": "#aaaaaa",
    "accent": "#ffffff",
    "success": "#28c83c",
    "error": "#dc281e",
    "warning": "#e6be1e",
    "muted": "#666666",
    "input_text": "#ffefD5",
    "input_bg": "#1a0000",
    "input_suggestion": "#3c2828",
    "input_ar_text": "#b8860b",
    "input_ar_bg": "#1a1200",
    "input_ar_suggestion": "#503c00",
    "dmz_active": "#b8860b",
    "dmz_inactive": "#320a0a",
    "cursor_color": "#e5ffff",
    "cursor_ar_color": "#e5edff",
    "active_cmd": "#786050",
    "bar_text_fill": "#101010",
    "bar_text_empty": "#666666",
    "bar_empty_bg": "#2a2a2a",
    "pending_cmd": "#787878",
}

# Cache: theme_name -> palette dict.
cache: dict[str, dict[str, str]] = {}


def resolve_theme(theme_name: str) -> dict[str, str]:
    """
    Resolve *theme_name* to a ``generate()`` color dict.

    :param theme_name: Name of a Textual built-in theme.
    :returns: Dict mapping token names to ``#rrggbb`` hex strings.
    """
    theme = textual.theme.BUILTIN_THEMES.get(theme_name)
    if theme is None:
        theme = textual.theme.BUILTIN_THEMES.get(DEFAULT_THEME)
    if theme is None:
        return {}
    cs = theme.to_color_system()
    return cs.generate()


def saved_theme_name(session_key: str) -> str:
    """
    Load the saved theme name for *session_key*, falling back to defaults.

    :param session_key: Session identifier (``host:port``), or empty string.
    :returns: Theme name string, or empty if nothing is saved.
    """
    if session_key:
        prefs = rooms.load_prefs(session_key)
        name = prefs.get("tui_theme", "")
        if isinstance(name, str) and name:
            return name
    prefs = rooms.load_prefs(client_tui_base.DEFAULTS_KEY)
    name = prefs.get("tui_theme", "")
    return name if isinstance(name, str) else ""


def get_repl_palette(session_key: str = "") -> dict[str, str]:
    """
    Return semantic REPL color names mapped to ``#rrggbb`` hex values.

    Loads the saved theme name from per-session or global preferences, then
    resolves Textual theme tokens into concrete hex colors.  Results are
    cached per theme name.

    :param session_key: Session identifier (``host:port``), or empty string.
    :returns: Dict of semantic color names to hex strings.
    """
    theme_name = saved_theme_name(session_key) or DEFAULT_THEME

    cached = cache.get(theme_name)
    if cached is not None:
        return cached

    generated = resolve_theme(theme_name)
    if not generated:
        cache[theme_name] = dict(FALLBACK)
        return cache[theme_name]

    palette: dict[str, str] = {}
    for semantic, token in TOKEN_MAP.items():
        value = generated.get(token)
        if value and isinstance(value, str) and value.startswith("#"):
            palette[semantic] = value[:7]
        else:
            palette[semantic] = FALLBACK.get(semantic, "#888888")

    cache[theme_name] = palette
    return palette


def invalidate_cache() -> None:
    """Clear the palette cache, forcing re-resolution on next call."""
    cache.clear()


def hex_to_rgb(hexcolor: str) -> tuple[int, int, int]:
    """
    Convert ``#rrggbb`` hex string to an ``(r, g, b)`` tuple.

    :param hexcolor: Color string like ``#1a0000``.
    :returns: Tuple of integers in ``[0, 255]``.
    """
    h = hexcolor.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
