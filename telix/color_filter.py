"""
ANSI color palette translation for MUD/BBS client output.

Translates basic 16-color SGR codes into 24-bit RGB equivalents from named
hardware palettes (VGA, xterm, C64), bypassing the terminal's custom palette
to display the colors the artist intended.

Features beyond the original telnetlib3 implementation:

- **Terminal color detection**: ``foreground_color`` field lets the client use
  the actual terminal foreground instead of palette white.
- **Force black background**: ``force_black_bg`` mode with Erase-to-End-of-Line
  injection ensures VGA black extends to line edges.
"""

from __future__ import annotations

# std imports
import re
from typing import NamedTuple

# 3rd party
import wcwidth.sgr_state

__all__ = ("PALETTES", "AtasciiControlFilter", "ColorConfig", "ColorFilter", "PetsciiColorFilter")

# Type alias for a 16-color palette: 16 (R, G, B) tuples indexed 0-15.
PaletteRGB = tuple[tuple[int, int, int], ...]

# Hardware color palettes.  Each defines exact RGB values for ANSI colors 0-15.
PALETTES: dict[str, PaletteRGB] = {
    "vga": (
        (0, 0, 0),
        (170, 0, 0),
        (0, 170, 0),
        (170, 85, 0),
        (0, 0, 170),
        (170, 0, 170),
        (0, 170, 170),
        (170, 170, 170),
        (85, 85, 85),
        (255, 85, 85),
        (85, 255, 85),
        (255, 255, 85),
        (85, 85, 255),
        (255, 85, 255),
        (85, 255, 255),
        (255, 255, 255),
    ),
    "xterm": (
        (0, 0, 0),
        (205, 0, 0),
        (0, 205, 0),
        (205, 205, 0),
        (0, 0, 238),
        (205, 0, 205),
        (0, 205, 205),
        (229, 229, 229),
        (127, 127, 127),
        (255, 0, 0),
        (0, 255, 0),
        (255, 255, 0),
        (92, 92, 255),
        (255, 0, 255),
        (0, 255, 255),
        (255, 255, 255),
    ),
    "c64": (
        (0, 0, 0),
        (255, 255, 255),
        (150, 40, 46),
        (91, 214, 206),
        (159, 45, 173),
        (65, 185, 54),
        (39, 36, 196),
        (239, 243, 71),
        (159, 72, 21),
        (94, 53, 0),
        (218, 95, 102),
        (71, 71, 71),
        (120, 120, 120),
        (145, 255, 132),
        (104, 100, 255),
        (174, 174, 174),
    ),
}

# Detect potentially incomplete escape sequence at end of a chunk.
_TRAILING_ESC = re.compile(r"\x1b(\[[\d;:]*)?$")


class ColorConfig(NamedTuple):
    """
    Configuration for ANSI color palette translation.

    :param palette_name: Name of the hardware palette to use (key in PALETTES).
    :param brightness: Brightness scale factor [0.0..1.0], where 1.0 is original.
    :param contrast: Contrast scale factor [0.0..1.0], where 1.0 is original.
    :param background_color: Forced background RGB as (R, G, B) tuple.
    :param ice_colors: When True, treat SGR 5 (blink) as bright background
        (iCE colors), promoting background 40-47 to palette 8-15.
    :param foreground_color: Detected terminal foreground RGB.  When set,
        used for default fg (SGR 0/39) instead of palette white.  No
        brightness/contrast adjustment is applied to detected colors.
    :param force_black_bg: When True, use (0,0,0) background and enable
        Erase-to-End-of-Line injection to extend black to line edges.
    """

    palette_name: str = "vga"
    brightness: float = 1.0
    contrast: float = 1.0
    background_color: tuple[int, int, int] = (0, 0, 0)
    ice_colors: bool = True
    foreground_color: tuple[int, int, int] | None = None
    force_black_bg: bool = False


def _sgr_code_to_palette_index(code: int) -> int | None:
    """
    Map a basic SGR color code to a palette index (0-15).

    :param code: SGR parameter value (30-37, 40-47, 90-97, or 100-107).
    :returns: Palette index 0-15, or None if not a basic color code.
    """
    if 30 <= code <= 37:
        return code - 30
    if 40 <= code <= 47:
        return code - 40
    if 90 <= code <= 97:
        return code - 90 + 8
    if 100 <= code <= 107:
        return code - 100 + 8
    return None


def _is_foreground_code(code: int) -> bool:
    """
    Return True if *code* is a foreground color SGR parameter.

    :param code: SGR parameter value.
    :returns: True for foreground codes (30-37, 90-97).
    """
    return (30 <= code <= 37) or (90 <= code <= 97)


def _adjust_color(r: int, g: int, b: int, brightness: float, contrast: float) -> tuple[int, int, int]:
    """
    Apply brightness and contrast scaling to an RGB color.

    :param r: Red channel (0-255).
    :param g: Green channel (0-255).
    :param b: Blue channel (0-255).
    :param brightness: Brightness factor [0.0..1.0].
    :param contrast: Contrast factor [0.0..1.0].
    :returns: Adjusted (R, G, B) tuple.
    """
    mid = 127.5
    r_f = mid + (r * brightness - mid) * contrast
    g_f = mid + (g * brightness - mid) * contrast
    b_f = mid + (b * brightness - mid) * contrast
    return (max(0, min(255, int(r_f + 0.5))), max(0, min(255, int(g_f + 0.5))), max(0, min(255, int(b_f + 0.5))))


class ColorFilter:
    """
    Stateful ANSI color palette translation filter.

    Translates basic 16-color ANSI SGR codes to 24-bit RGB equivalents from a named hardware palette, with
    brightness/contrast adjustment and background color enforcement.

    The filter is designed to process chunked text (as received from a telnet connection) and correctly handles escape
    sequences split across chunk boundaries.

    :param config: Color configuration parameters.
    """

    def __init__(self, config: ColorConfig) -> None:
        """Initialize with the given color configuration."""
        self._config = config
        palette = PALETTES[config.palette_name]
        self._adjusted: list[tuple[int, int, int]] = [
            _adjust_color(r, g, b, config.brightness, config.contrast) for r, g, b in palette
        ]
        bg = config.background_color
        self._bg_sgr = f"\x1b[48;2;{bg[0]};{bg[1]};{bg[2]}m"
        if config.foreground_color is not None:
            fg = config.foreground_color
        else:
            fg = self._adjusted[7]
        self._fg_sgr = f"\x1b[38;2;{fg[0]};{fg[1]};{fg[2]}m"
        self._reset_bg_parts = ["48", "2", str(bg[0]), str(bg[1]), str(bg[2])]
        self._reset_fg_parts = ["38", "2", str(fg[0]), str(fg[1]), str(fg[2])]
        self._fg_color = fg
        self._current_fg: tuple[int, int, int] = fg
        self._current_bg: tuple[int, int, int] = bg
        self._buffer = ""
        self._initial = True
        self._bold = False
        self._blink = False
        self._fg_idx = 7  # current fg palette index (0-15), -1 for extended

    def filter(self, text: str) -> str:
        """
        Transform SGR sequences in *text* using the configured palette.

        :param text: Input text, possibly containing ANSI escape sequences.
        :returns: Text with basic colors replaced by 24-bit RGB equivalents.
        """
        if self._buffer:
            text = self._buffer + text
            self._buffer = ""

        match = _TRAILING_ESC.search(text)
        if match:
            self._buffer = match.group()
            text = text[: match.start()]

        if not text:
            return ""

        result = wcwidth.sgr_state._SGR_PATTERN.sub(self._replace_sgr, text)

        if self._initial:
            self._initial = False
            result = self._bg_sgr + result
        return result

    def _replace_sgr(self, match: re.Match[str]) -> str:
        r"""
        Regex replacement callback for a single SGR sequence.

        Tracks bold state across calls so that ``\x1b[1;30m`` (bold + black) uses the bright palette entry (index 8)
        instead of pure black.
        """
        params_str = match.group(1)

        if not params_str:
            self._bold = False
            self._blink = False
            self._fg_idx = 7
            self._current_fg = self._fg_color
            self._current_bg = self._config.background_color
            return f"\x1b[0;{';'.join(self._reset_bg_parts)};{';'.join(self._reset_fg_parts)}m"

        if ":" in params_str:
            return match.group()

        parts = params_str.split(";")
        output_parts: list[str] = []
        i = 0

        seq_sets_bold = False
        seq_sets_blink = False
        seq_has_fg = False
        for part in parts:
            try:
                val = int(part) if part else 0
            except ValueError:
                continue
            if val == 1:
                seq_sets_bold = True
            elif val == 5:
                seq_sets_blink = True
            if (30 <= val <= 37) or (90 <= val <= 97) or val in (38, 39):
                seq_has_fg = True

        bold = self._bold or seq_sets_bold
        ice = self._config.ice_colors
        blink = self._blink or (seq_sets_blink and ice)

        while i < len(parts):
            try:
                p = int(parts[i]) if parts[i] else 0
            except ValueError:
                output_parts.append(parts[i])
                i += 1
                continue

            if p == 0:
                bold = False
                blink = False
                output_parts.append("0")
                output_parts.extend(self._reset_bg_parts)
                output_parts.extend(self._reset_fg_parts)
                self._fg_idx = 7
                self._current_fg = self._fg_color
                self._current_bg = self._config.background_color
                i += 1
                continue

            if p == 1:
                bold = True
                output_parts.append("1")
                if not seq_has_fg and 0 <= self._fg_idx <= 7:
                    bright_idx = self._fg_idx + 8
                    r, g, b = self._adjusted[bright_idx]
                    self._current_fg = (r, g, b)
                    output_parts.extend(["38", "2", str(r), str(g), str(b)])
                i += 1
                continue
            if p == 22:
                bold = False
                output_parts.append("22")
                if not seq_has_fg and 0 <= self._fg_idx <= 7:
                    if self._fg_idx == 7 and self._config.foreground_color is not None:
                        r, g, b = self._fg_color
                    else:
                        r, g, b = self._adjusted[self._fg_idx]
                    self._current_fg = (r, g, b)
                    output_parts.extend(["38", "2", str(r), str(g), str(b)])
                i += 1
                continue

            if p == 5:
                if ice:
                    blink = True
                else:
                    output_parts.append("5")
                i += 1
                continue
            if p == 25:
                blink = False
                if not ice:
                    output_parts.append("25")
                i += 1
                continue

            if p == 7:
                r, g, b = self._current_bg
                fg_r, fg_g, fg_b = self._current_fg
                output_parts.extend(["38", "2", str(r), str(g), str(b), "48", "2", str(fg_r), str(fg_g), str(fg_b)])
                i += 1
                continue
            if p == 27:
                r, g, b = self._current_fg
                bg_r, bg_g, bg_b = self._current_bg
                output_parts.extend(["38", "2", str(r), str(g), str(b), "48", "2", str(bg_r), str(bg_g), str(bg_b)])
                i += 1
                continue

            if p in (38, 48):
                if p == 38:
                    self._fg_idx = -1
                start_i = i
                i += 1
                if i < len(parts):
                    try:
                        mode = int(parts[i]) if parts[i] else 0
                    except ValueError:
                        mode = 0
                    i += 1
                    if mode == 5 and i < len(parts):
                        i += 1
                    elif mode == 2 and i + 2 < len(parts):
                        i += 3
                output_parts.extend(parts[start_i:i])
                continue

            if p == 39:
                self._fg_idx = 7
                r, g, b = self._fg_color
                self._current_fg = (r, g, b)
                output_parts.extend(["38", "2", str(r), str(g), str(b)])
                i += 1
                continue
            if p == 49:
                bg = self._config.background_color
                self._current_bg = bg
                output_parts.extend(["48", "2", str(bg[0]), str(bg[1]), str(bg[2])])
                i += 1
                continue

            idx = _sgr_code_to_palette_index(p)
            if idx is not None:
                is_fg = _is_foreground_code(p)
                if is_fg:
                    self._fg_idx = idx
                if is_fg and bold and 30 <= p <= 37:
                    idx += 8
                if not is_fg and blink and 40 <= p <= 47:
                    idx += 8
                r, g, b = self._adjusted[idx]
                if is_fg:
                    self._current_fg = (r, g, b)
                    output_parts.extend(["38", "2", str(r), str(g), str(b)])
                else:
                    self._current_bg = (r, g, b)
                    output_parts.extend(["48", "2", str(r), str(g), str(b)])
            else:
                output_parts.append(str(p))
            i += 1

        self._bold = bold
        self._blink = blink

        result = f"\x1b[{';'.join(output_parts)}m" if output_parts else ""
        return result

    def flush(self) -> str:
        """
        Flush any buffered partial escape sequence.

        :returns: Buffered content (may be an incomplete escape sequence).
        """
        result = self._buffer
        self._buffer = ""
        return result


# PETSCII decoded control character -> VIC-II palette index (0-15).
_PETSCII_COLOR_CODES: dict[str, int] = {
    "\x05": 1,  # WHT (white)
    "\x1c": 2,  # RED
    "\x1e": 5,  # GRN (green)
    "\x1f": 6,  # BLU (blue)
    "\x81": 8,  # ORN (orange)
    "\x90": 0,  # BLK (black)
    "\x95": 9,  # BRN (brown)
    "\x96": 10,  # LRD (pink / light red)
    "\x97": 11,  # GR1 (dark grey)
    "\x98": 12,  # GR2 (grey)
    "\x99": 13,  # LGR (light green)
    "\x9a": 14,  # LBL (light blue)
    "\x9b": 15,  # GR3 (light grey)
    "\x9c": 4,  # PUR (purple)
    "\x9e": 7,  # YEL (yellow)
    "\x9f": 3,  # CYN (cyan)
}

# PETSCII cursor/screen control codes -> ANSI escape sequences.
_PETSCII_CURSOR_CODES: dict[str, str] = {
    "\x11": "\x1b[B",  # cursor down
    "\x91": "\x1b[A",  # cursor up
    "\x1d": "\x1b[C",  # cursor right
    "\x9d": "\x1b[D",  # cursor left
    "\x13": "\x1b[H",  # HOME (cursor to top-left)
    "\x93": "\x1b[2J",  # CLR (clear screen)
    "\x14": "\x08\x1b[P",  # DEL (destructive backspace)
}

# All PETSCII control chars handled by the filter.
_PETSCII_FILTER_CHARS = frozenset(_PETSCII_COLOR_CODES) | frozenset(_PETSCII_CURSOR_CODES) | {"\x12", "\x92"}

_PETSCII_CTRL_RE = re.compile("[" + re.escape("".join(sorted(_PETSCII_FILTER_CHARS))) + "]")


class PetsciiColorFilter:
    r"""
    Translate PETSCII control codes to ANSI sequences.

    PETSCII uses single-byte control codes embedded in the text stream for
    color changes, cursor movement, and screen control.  This filter
    translates them to ANSI equivalents:

    - **Colors**: 16 VIC-II palette colors -> ``\x1b[38;2;R;G;Bm`` (24-bit RGB)
    - **Reverse video**: RVS ON/OFF -> ``\x1b[7m`` / ``\x1b[27m``
    - **Cursor**: up/down/left/right -> ``\x1b[A/B/C/D``
    - **Screen**: HOME -> ``\x1b[H``, CLR -> ``\x1b[2J``
    - **DEL**: destructive backspace -> ``\x08\x1b[P``

    :param brightness: Brightness factor [0.0..1.0] for palette adjustment.
    :param contrast: Contrast factor [0.0..1.0] for palette adjustment.
    """

    def __init__(self, brightness: float = 1.0, contrast: float = 1.0) -> None:
        """Initialize PETSCII filter with optional brightness/contrast."""
        self._adjusted: list[tuple[int, int, int]] = [
            _adjust_color(r, g, b, brightness, contrast) for r, g, b in PALETTES["c64"]
        ]

    def _sgr_for_index(self, idx: int) -> str:
        """Return a 24-bit foreground SGR sequence for palette *idx*."""
        r, g, b = self._adjusted[idx]
        return f"\x1b[38;2;{r};{g};{b}m"

    def filter(self, text: str) -> str:
        """
        Replace PETSCII control codes with ANSI sequences.

        :param text: Decoded PETSCII text (Unicode string).
        :returns: Text with PETSCII controls translated to ANSI.
        """
        if not _PETSCII_CTRL_RE.search(text):
            return text
        return _PETSCII_CTRL_RE.sub(self._replace, text)

    def _replace(self, match: re.Match[str]) -> str:
        """Regex callback for a single PETSCII control character."""
        ch = match.group()
        idx = _PETSCII_COLOR_CODES.get(ch)
        if idx is not None:
            return self._sgr_for_index(idx)
        cursor = _PETSCII_CURSOR_CODES.get(ch)
        if cursor is not None:
            return cursor
        if ch == "\x12":
            return "\x1b[7m"
        if ch == "\x92":
            return "\x1b[27m"
        return ""

    def flush(self) -> str:
        """
        Flush buffered state.

        :returns: Always ``""``.
        """
        return ""


# ATASCII decoded control character glyphs -> ANSI terminal sequences.
_ATASCII_CONTROL_CODES: dict[str, str] = {
    "\u25c0": "\x08\x1b[P",  # ◀  backspace/delete (0x7E / 0xFE)
    "\u25b6": "\t",  # ▶  tab (0x7F / 0xFF)
    "\u21b0": "\x1b[2J\x1b[H",  # ↰  clear screen (0x7D / 0xFD)
    "\u2191": "\x1b[A",  # ↑  cursor up (0x1C / 0x9C)
    "\u2193": "\x1b[B",  # ↓  cursor down (0x1D / 0x9D)
    "\u2190": "\x1b[D",  # ←  cursor left (0x1E / 0x9E)
    "\u2192": "\x1b[C",  # ->  cursor right (0x1F / 0x9F)
}

_ATASCII_CTRL_RE = re.compile("[" + re.escape("".join(sorted(_ATASCII_CONTROL_CODES))) + "]")


class AtasciiControlFilter:
    r"""
    Translate decoded ATASCII control character glyphs to ANSI sequences.

    The ``atascii`` codec decodes ATASCII control bytes into Unicode glyphs
    (e.g. byte 0x7E -> U+25C0 ◀).  This filter replaces those glyphs with
    the ANSI terminal sequences that produce the intended effect:

    - **Backspace/delete**: ◀ -> ``\x08\x1b[P`` (destructive backspace)
    - **Tab**: ▶ -> ``\t``
    - **Clear screen**: ↰ -> ``\x1b[2J\x1b[H``
    - **Cursor movement**: ↑↓←-> -> ``\x1b[A/B/D/C``
    """

    def filter(self, text: str) -> str:
        """
        Replace ATASCII control glyphs with ANSI sequences.

        :param text: Decoded ATASCII text (Unicode string).
        :returns: Text with control glyphs translated to ANSI.
        """
        if not _ATASCII_CTRL_RE.search(text):
            return text
        return _ATASCII_CTRL_RE.sub(self._replace, text)

    @staticmethod
    def _replace(match: re.Match[str]) -> str:
        """Regex callback for a single ATASCII control glyph."""
        return _ATASCII_CONTROL_CODES.get(match.group(), "")

    @staticmethod
    def flush() -> str:
        """
        Flush buffered state.

        :returns: Always ``""``.
        """
        return ""
