## Progress Bar Editor

Configure GMCP-driven progress bars for the toolbar.  Each bar maps
a GMCP package field pair (value + max) to a colored bar display.

### Table Columns

| Column | Meaning |
|--------|---------|
| **#** | Display order |
| **Name** | Label shown on the toolbar |
| **Source** | GMCP package name (e.g. `Char.Vitals`) |
| **Enabled** | Whether the bar is active |
| **Color** | Color mode (`theme` or `custom`) |

### Form Fields

- **Name** — display label for the bar
- **Enabled** — toggle the bar on/off
- **GMCP Pkg** — the GMCP package containing the fields
  (e.g. `Char.Vitals`, `Char.Status`)
- **Value Field** — field name for the current value (e.g. `hp`)
- **Max Field** — field name for the maximum value (e.g. `maxhp`)
- **Color Mode** — `Theme` uses your TUI theme accent color;
  `Custom` lets you choose specific max/min colors
- **Max Color** — bar color at 100% (custom mode only)
- **Min Color** — bar color at 0% (custom mode only)
- **Text Color** — foreground color for text overlaid on the bar.
  `auto` (default) uses dark text on the filled portion and grey on
  the empty portion.  Choose a theme or custom color to override.
- **Path** — `Shortest` or `Longest` hue arc between colors
- **Preview** — live preview of the bar at the chosen percentage
- **Value %** — preview percentage (0-100)

### Side

Each bar has a **Side** setting (`Left` or `Right`) that controls
which side of the toolbar it appears on.  GMCP bars default to
`Left`; the built-in `<Travel>` bar defaults to `Right`.

### Built-in `<Travel>` Bar

The `<Travel>` bar is a built-in entry that displays progress for
randomwalk and autodiscover operations.  It is automatically
included when you press **Detect** and cannot be duplicated.

Unlike GMCP bars, the `<Travel>` bar has no GMCP source — its
Source, Value, and Max fields are disabled in the editor.  You
can still customize its colors, color path, side, and enabled
state like any other bar.

### Auto-Detection

Press **Detect** to scan GMCP data for value/max field pairs.
Detection looks for:

- The built-in `<Travel>` bar (always included)
- Standard HP/MP/XP aliases in `Char.Vitals` and `Char.Status`
- `MaxSomething` / `Something` pairs
- `SomethingMax` / `Something` pairs

Detected bars that already exist are skipped.  HP, MP, and XP
are enabled by default; other detected bars are disabled.

### Color Modes

**Theme** — interpolates between the TUI accent color and its
hue complement.  Changes automatically when you switch themes.

**Custom** — interpolates between a chosen max color (full bar)
and min color (empty bar) through HSV color space using the
selected hue path (shortest or longest arc).

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| **+** / **=** | Move selected bar up (higher priority) |
| **-** | Move selected bar down |
| **Enter** | Edit selected / submit form |
| **Escape** | Cancel form / close editor |
