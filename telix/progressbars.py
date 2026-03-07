"""
Progress bar configuration model for the GMCP vitals toolbar.

Provides auto-detection of progress bar candidates from GMCP data, persistent configuration via JSON, and color
interpolation for custom bar rendering.
"""

# std imports
import os
import json
import typing

# 3rd party
import textual.app
import textual.theme
from rich.color import ANSI_COLOR_NAMES, Color

# local
from . import paths, client_repl_render

__all__ = (
    "CURATED_COLORS",
    "TRAVEL_BAR_NAME",
    "BarConfig",
    "bar_color_at",
    "detect_progressbars",
    "load_progressbars",
    "resolve_text_color_hex",
    "save_progressbars",
)

TRAVEL_BAR_NAME = "<Travel>"  #: Name of the built-in travel progress bar.


class BarConfig(typing.NamedTuple):
    """Configuration for a single toolbar progress bar."""

    name: str
    gmcp_package: str
    value_field: str
    max_field: str
    enabled: bool = True
    color_mode: str = "theme"
    color_name_max: str = "success"
    color_name_min: str = "error"
    color_path: str = "shortest"
    text_color_fill: str = "auto"
    text_color_empty: str = "auto"
    display_order: int = 0
    side: str = "left"


#: All Rich named colors for the custom color picker.
def all_rich_colors() -> list[str]:
    """Return all Rich named colors sorted alphabetically."""
    return sorted(ANSI_COLOR_NAMES.keys())


CURATED_COLORS: list[str] = all_rich_colors()


def get_theme_colors() -> dict[str, str]:
    """
    Return all theme color names mapped to ``#rrggbb`` hex values.

    Uses the running Textual app's theme when available, otherwise
    falls back to ``textual-dark`` defaults.
    """
    app = getattr(textual.app.App, "current", None)
    if app is not None:
        cs = app.current_theme
        if cs is not None:
            return {
                k: v
                for k, v in sorted(cs.generate().items())
                if not any(
                    x in k
                    for x in (
                        "cursor",
                        "button",
                        "footer",
                        "style",
                        "hover",
                        "disabled",
                        "scrollbar",
                        "input",
                        "link",
                        "text-",
                        "markdown",
                        "tooltip",
                    )
                )
                and len(v) in (7, 9)
                and v.startswith("#")
            }
    # Fallback when no app is running.
    cs = textual.theme.BUILTIN_THEMES["textual-dark"].to_color_system()
    return {
        k: v
        for k, v in sorted(cs.generate().items())
        if not any(
            x in k
            for x in (
                "cursor",
                "button",
                "footer",
                "style",
                "hover",
                "disabled",
                "scrollbar",
                "input",
                "link",
                "text-",
                "markdown",
                "tooltip",
            )
        )
        and len(v) in (7, 9)
        and v.startswith("#")
    }


# Standard HP/MP/XP field aliases from Char.Vitals and Char.Status.
HP_ALIASES: dict[str, tuple[str, ...]] = {"hp": ("maxhp", "maxHP", "max_hp"), "HP": ("maxHP", "maxhp", "max_hp")}
MP_ALIASES: dict[str, tuple[str, ...]] = {
    "mp": ("maxmp", "maxMP", "max_mp", "maxsp", "maxSP"),
    "MP": ("maxMP", "maxmp", "max_mp"),
    "mana": ("maxmana", "max_mana"),
    "sp": ("maxsp", "maxSP", "max_sp"),
    "SP": ("maxSP", "maxsp", "max_sp"),
}
XP_ALIASES: dict[str, tuple[str, ...]] = {
    "xp": ("maxxp", "maxXP", "max_xp", "maxexp"),
    "XP": ("maxXP", "maxxp", "max_xp"),
    "experience": ("maxexp", "max_experience", "maxexperience"),
}


def load_progressbars(path: str, session_key: str) -> list[BarConfig]:
    """
    Load progress bar configs for a session from a JSON file.

    :param path: Path to the progressbars JSON file.
    :param session_key: Session identifier (``host:port``).
    :returns: List of :class:`BarConfig` instances.
    """
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        data: dict[str, typing.Any] = json.load(fh)
    session_data: dict[str, typing.Any] = data.get(session_key, {})
    entries: list[dict[str, typing.Any]] = session_data.get("bars", [])
    return [dict_to_bar(e, i) for i, e in enumerate(entries)]


def save_progressbars(path: str, session_key: str, bars: list[BarConfig]) -> None:
    """
    Save progress bar configs for a session to a JSON file.

    Other sessions' data in the file is preserved.

    :param path: Path to the progressbars JSON file.
    :param session_key: Session identifier (``host:port``).
    :param bars: List of :class:`BarConfig` instances.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    data: dict[str, typing.Any] = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    entries = [bar_to_dict(b) for b in bars]
    data[session_key] = {"bars": entries}
    paths.atomic_json_write(path, data)


def detect_progressbars(gmcp_data: dict[str, typing.Any]) -> list[BarConfig]:
    """
    Scan GMCP data for progress bar candidates.

    Detects value/max pairs by matching field names:

    - ``MaxSomething`` + ``Something``
    - ``SomethingMax`` + ``Something``
    - Standard HP/MP/XP aliases from ``Char.Vitals`` / ``Char.Status``

    HP, MP, and XP are enabled by default; all others are disabled.

    :param gmcp_data: Current ``ctx.gmcp_data`` dict.
    :returns: List of detected :class:`BarConfig` instances.
    """
    if not gmcp_data:
        return []
    bars: list[BarConfig] = [
        BarConfig(
            name=TRAVEL_BAR_NAME,
            gmcp_package="",
            value_field="",
            max_field="",
            enabled=True,
            color_mode="custom",
            color_name_max="dark_cyan",
            color_name_min="dark_orange",
            color_path="shortest",
            display_order=0,
            side="right",
        )
    ]
    seen: set[tuple[str, str, str]] = set()

    # Standard vitals first.
    detect_standard(gmcp_data, bars, seen)
    # Then scan all packages for Max*/prefix/suffix pairs.
    for pkg_name, pkg_data in gmcp_data.items():
        if not isinstance(pkg_data, dict):
            continue
        detect_pairs(pkg_name, pkg_data, bars, seen)

    for i, bar in enumerate(bars):
        bars[i] = bar._replace(display_order=i)
    return bars


def get_theme_color_hex(name: str) -> str | None:
    """
    Resolve a Textual theme color name to ``#rrggbb``.

    :param name: Theme color name (e.g. ``"success"``, ``"error-lighten-2"``).
    :returns: Hex color string, or ``None`` if not a theme color.
    """
    theme_colors = get_theme_colors()
    return theme_colors.get(name)


def resolve_text_color_hex(name: str) -> str | None:
    """
    Resolve a text color name to ``#rrggbb``, or ``None`` for ``"auto"``.

    :param name: Color name or ``"auto"`` for default behavior.
    :returns: Hex color string, or ``None`` if *name* is ``"auto"``.
    """
    if name == "auto":
        return None
    r, g, b = resolve_color_rgb(name)
    return f"#{r:02x}{g:02x}{b:02x}"


def resolve_color_rgb(name: str) -> tuple[int, int, int]:
    """Resolve a color name (theme or Rich named) to ``(r, g, b)``."""
    hex_val = get_theme_color_hex(name)
    if hex_val is not None:
        h = hex_val.lstrip("#")
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    return named_color_rgb(name)


def bar_color_at(fraction: float, bar: BarConfig, theme_accent: tuple[int, int, int] | None = None) -> str:
    """
    Return an ``#rrggbb`` hex color for a bar at *fraction* full.

    Both theme and custom modes use ``color_name_min`` / ``color_name_max``
    to define the gradient endpoints.  Theme mode resolves names from the
    active Textual theme; custom mode uses Rich named colors.

    :param fraction: 0.0 (empty) to 1.0 (full).
    :param bar: Bar configuration.
    :param theme_accent: Unused, kept for API compatibility.
    :returns: Hex color string.
    """
    fraction = max(0.0, min(1.0, fraction))
    max_rgb = resolve_color_rgb(bar.color_name_max)
    min_rgb = resolve_color_rgb(bar.color_name_min)
    hsv_max = client_repl_render.rgb_to_hsv(*max_rgb)
    hsv_min = client_repl_render.rgb_to_hsv(*min_rgb)
    path = bar.color_path if bar.color_mode == "custom" else "shortest"
    h, s, v = lerp_hsv_path(hsv_min, hsv_max, fraction, path)
    r, g, b = client_repl_render.hsv_to_rgb(h, s, v)
    return f"#{r:02x}{g:02x}{b:02x}"


def lerp_hsv_path(
    hsv1: tuple[float, float, float], hsv2: tuple[float, float, float], t: float, path: str
) -> tuple[float, float, float]:
    """Interpolate HSV using shortest or longest hue arc."""
    h1, s1, v1 = hsv1
    h2, s2, v2 = hsv2
    dh = (h2 - h1) % 360.0
    if path == "shortest":
        if dh > 180.0:
            dh -= 360.0
    elif dh <= 180.0:
        dh = dh - 360.0
    h = (h1 + t * dh) % 360.0
    return (h, s1 + t * (s2 - s1), v1 + t * (v2 - v1))


def named_color_rgb(name: str) -> tuple[int, int, int]:
    """Convert a Rich named color to an (r, g, b) tuple."""
    color = Color.parse(name)
    triplet = color.get_truecolor()
    return (triplet.red, triplet.green, triplet.blue)


def detect_standard(gmcp_data: dict[str, typing.Any], bars: list[BarConfig], seen: set[tuple[str, str, str]]) -> None:
    """Detect standard HP/MP/XP bars from known GMCP packages."""
    vitals = gmcp_data.get("Char.Vitals")
    if isinstance(vitals, dict):
        for alias_map, bar_name, defaults in (
            (HP_ALIASES, "HP", ("green", "red")),
            (MP_ALIASES, "MP", ("dodger_blue2", "gold1")),
        ):
            try_aliases(vitals, "Char.Vitals", alias_map, bar_name, defaults, True, bars, seen)

    status = gmcp_data.get("Char.Status")
    if isinstance(status, dict):
        try_aliases(status, "Char.Status", XP_ALIASES, "XP", ("purple", "cyan"), True, bars, seen)


def is_numeric(value: typing.Any) -> bool:
    """Return ``True`` if *value* is numeric (int, float, or numeric string)."""
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        try:
            float(value)
            return True
        except ValueError:
            return False
    return False


def try_aliases(
    pkg_data: dict[str, typing.Any],
    pkg_name: str,
    alias_map: dict[str, tuple[str, ...]],
    bar_name: str,
    defaults: tuple[str, str],
    enabled: bool,
    bars: list[BarConfig],
    seen: set[tuple[str, str, str]],
) -> None:
    """Try known field aliases and add a bar if a matching pair is found."""
    for val_field, max_fields in alias_map.items():
        if val_field not in pkg_data:
            continue
        for max_field in max_fields:
            if max_field in pkg_data and is_numeric(pkg_data[max_field]):
                key = (pkg_name, val_field, max_field)
                if key not in seen:
                    seen.add(key)
                    bars.append(
                        BarConfig(
                            name=bar_name,
                            gmcp_package=pkg_name,
                            value_field=val_field,
                            max_field=max_field,
                            enabled=enabled,
                            color_mode="theme",
                            color_name_max=defaults[0],
                            color_name_min=defaults[1],
                        )
                    )
                return


def detect_pairs(
    pkg_name: str, pkg_data: dict[str, typing.Any], bars: list[BarConfig], seen: set[tuple[str, str, str]]
) -> None:
    """Scan a GMCP package for Max*/prefix/suffix value/max pairs."""
    keys = list(pkg_data.keys())
    keys_lower = {k.lower(): k for k in keys}
    for key in keys:
        lower = key.lower()
        # MaxSomething pattern
        if lower.startswith("max") and len(lower) > 3:
            base_lower = lower[3:]
            if base_lower in keys_lower:
                val_field = keys_lower[base_lower]
                if not is_numeric(pkg_data[val_field]):
                    continue
                trio = (pkg_name, val_field, key)
                if trio not in seen:
                    seen.add(trio)
                    bars.append(
                        BarConfig(
                            name=val_field, gmcp_package=pkg_name, value_field=val_field, max_field=key, enabled=False
                        )
                    )
        # SomethingMax pattern
        if lower.endswith("max") and len(lower) > 3:
            base_lower = lower[:-3]
            if base_lower in keys_lower:
                val_field = keys_lower[base_lower]
                if not is_numeric(pkg_data[val_field]):
                    continue
                trio = (pkg_name, val_field, key)
                if trio not in seen:
                    seen.add(trio)
                    bars.append(
                        BarConfig(
                            name=val_field, gmcp_package=pkg_name, value_field=val_field, max_field=key, enabled=False
                        )
                    )


def dict_to_bar(entry: dict[str, typing.Any], idx: int) -> BarConfig:
    """Convert a JSON dict to a :class:`BarConfig`."""
    return BarConfig(
        name=str(entry.get("name", "")),
        gmcp_package=str(entry.get("gmcp_package", "")),
        value_field=str(entry.get("value_field", "")),
        max_field=str(entry.get("max_field", "")),
        enabled=bool(entry.get("enabled", True)),
        color_mode=str(entry.get("color_mode", "theme")),
        color_name_max=str(entry.get("color_name_max", "green")),
        color_name_min=str(entry.get("color_name_min", "red")),
        color_path=str(entry.get("color_path", "shortest")),
        text_color_fill=str(entry.get("text_color_fill", "auto")),
        text_color_empty=str(entry.get("text_color_empty", "auto")),
        display_order=int(entry.get("display_order", idx)),
        side=str(entry.get("side", "left")),
    )


def bar_to_dict(bar: BarConfig) -> dict[str, typing.Any]:
    """Convert a :class:`BarConfig` to a JSON-serializable dict."""
    result: dict[str, typing.Any] = {
        "name": bar.name,
        "gmcp_package": bar.gmcp_package,
        "value_field": bar.value_field,
        "max_field": bar.max_field,
        "enabled": bar.enabled,
        "color_mode": bar.color_mode,
        "color_name_max": bar.color_name_max,
        "color_name_min": bar.color_name_min,
        "color_path": bar.color_path,
        "display_order": bar.display_order,
    }
    if bar.side != "left":
        result["side"] = bar.side
    if bar.text_color_fill != "auto":
        result["text_color_fill"] = bar.text_color_fill
    if bar.text_color_empty != "auto":
        result["text_color_empty"] = bar.text_color_empty
    return result
