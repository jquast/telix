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
    "muted": "foreground-disabled",
    "input_text": "foreground",
    "input_bg": "background",
    "input_suggestion": "foreground-disabled",
    "input_ar_text": "warning",
    "input_ar_bg": "warning-darken-2",
    "input_ar_suggestion": "warning-darken-2",
    "dmz_active": "warning",
    "dmz_inactive": "panel",
    "cursor_color": "foreground",
    "cursor_ar_color": "warning-lighten-1",
    "bar_text_fill": "background",
    "bar_text_empty": "secondary",
    "bar_empty_bg": "surface",
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
    "muted": "#555555",
    "input_text": "#ffefD5",
    "input_bg": "#000000",
    "input_suggestion": "#555555",
    "input_ar_text": "#b8860b",
    "input_ar_bg": "#1a1400",
    "input_ar_suggestion": "#503c00",
    "dmz_active": "#b8860b",
    "dmz_inactive": "#3a3a3a",
    "cursor_color": "#e5ffff",
    "cursor_ar_color": "#e5edff",
    "bar_text_fill": "#101010",
    "bar_text_empty": "#666666",
    "bar_empty_bg": "#2a2a2a",
}

# Cache: theme_name -> palette dict.
cache: dict[str, dict[str, str]] = {}


def invert_hex(hexcolor: str) -> str:
    """
    Return the RGB inverse of *hexcolor*.

    :param hexcolor: Color string like ``#1a0000``.
    :returns: Inverted color as ``#rrggbb``.
    """
    r, g, b = hex_to_rgb(hexcolor)
    return f"#{255 - r:02x}{255 - g:02x}{255 - b:02x}"


def blend_hex(c1: str, c2: str, t: float) -> str:
    """
    Linearly blend *c1* towards *c2* by fraction *t*.

    :param c1: Start color as ``#rrggbb``.
    :param c2: End color as ``#rrggbb``.
    :param t: Blend fraction in ``[0, 1]`` (0 = c1, 1 = c2).
    :returns: Blended color as ``#rrggbb``.
    """
    r1, g1, b1 = hex_to_rgb(c1)
    r2, g2, b2 = hex_to_rgb(c2)
    r = round(r1 + (r2 - r1) * t)
    g = round(g1 + (g2 - g1) * t)
    b = round(b1 + (b2 - b1) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def compute_derived(palette: dict[str, str]) -> None:
    """
    Compute contrast-derived palette entries in-place from ``input_ar_bg``.

    ``active_cmd``, ``pending_cmd``, and ``input_ar_suggestion`` are set to
    blends of the autoreply background toward its RGB inverse so they are
    always readable regardless of theme.

    :param palette: Partially-resolved palette dict; modified in place.
    """
    ar_bg = palette.get("input_ar_bg", "#000000")
    ar_inv = invert_hex(ar_bg)
    palette["active_cmd"] = ar_inv
    palette["pending_cmd"] = blend_hex(ar_bg, ar_inv, 0.6)
    palette["input_ar_suggestion"] = blend_hex(ar_bg, ar_inv, 0.35)


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
        fallback = dict(FALLBACK)
        compute_derived(fallback)
        cache[theme_name] = fallback
        return fallback

    # Resolve background first — needed to blend alpha-bearing hex values.
    bg_token = TOKEN_MAP["background"]
    bg_raw = generated.get(bg_token, "")
    bg_hex = bg_raw[:7] if bg_raw.startswith("#") else FALLBACK["background"]

    palette: dict[str, str] = {}
    for semantic, token in TOKEN_MAP.items():
        value = generated.get(token)
        if value and isinstance(value, str) and value.startswith("#"):
            if len(value) >= 9:
                # Alpha channel present (#rrggbbaa) — blend against background.
                alpha = int(value[7:9], 16) / 255.0
                fg = hex_to_rgb(value[:7])
                bg = hex_to_rgb(bg_hex)
                r, g, b = (int(f * alpha + b * (1 - alpha)) for f, b in zip(fg, bg, strict=False))
                palette[semantic] = f"#{r:02x}{g:02x}{b:02x}"
            else:
                palette[semantic] = value[:7]
        else:
            palette[semantic] = FALLBACK.get(semantic, "#888888")

    compute_derived(palette)
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
