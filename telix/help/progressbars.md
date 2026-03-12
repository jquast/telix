## Progress Bar Editor

**Default key:** Alt + B

Configure GMCP-driven progress bars for the toolbar.  Each bar maps
a GMCP package field pair (value + max) to a colored bar display.

### Table Columns

| Column | Meaning |
|--------|---------|
| **#** | Display order |
| **Name** | Label shown on the toolbar |
| **Source** | GMCP package name (e.g. `Char.Vitals`) |
| **Value** | Field name for the current value |
| **Max** | Field name for the maximum value |
| **Enabled** | Whether the bar is active |
| **Color** | Color gradient swatch |

### Buttons

| Button | Action |
|--------|--------|
| **Add** | Create a new progress bar |
| **Edit** | Edit the selected bar |
| **Copy** | Duplicate the selected bar |
| **Detect** | Auto-detect bars from current GMCP data |
| **Help** | Open this help screen |
| **Save** | Save all changes to disk and close |
| **Cancel** | Discard changes and close |

### Form Fields

- **Name** -- display label for the bar
- **Enabled** -- toggle the bar on/off
- **Source** -- the GMCP package containing the fields
  (e.g. `Char.Vitals`, `Char.Status`)
- **Val/Max** -- field names for the current value and maximum value
  (e.g. `hp` and `maxhp`); two dropdowns on the same row
- **Color Mode** -- `Theme` uses your TUI theme accent color;
  `Custom` lets you choose specific max/min colors
- **Min** -- bar color at 0% and text color on the filled portion
  (custom mode: two dropdowns; theme mode: theme color choices)
- **Max** -- bar color at 100% and text color on the empty portion
  (custom mode: two dropdowns; theme mode: theme color choices)
- **Path** -- `Shortest` or `Longest` hue arc between colors
- **Preview** -- animated live preview of the bar cycling through 0-100%

### Side

Each bar has a **Side** setting (`Left` or `Right`) that controls
which side of the toolbar it appears on.  GMCP bars default to
`Left`; the built-in `<Travel>` bar defaults to `Right`.

### Built-in `<Travel>` Bar

The `<Travel>` bar is a built-in entry that displays progress for
randomwalk and autodiscover operations.  It is automatically
included when you press **Detect** and cannot be duplicated.

Unlike GMCP bars, the `<Travel>` bar has no GMCP source -- its
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

**Theme** -- interpolates between the TUI accent color and its
hue complement.  Changes automatically when you switch themes.

**Custom** -- interpolates between a chosen max color (full bar)
and min color (empty bar) through HSV color space using the
selected hue path (shortest or longest arc).

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| **+** / **=** | Move selected bar up (higher priority) |
| **-** | Move selected bar down |
| **Enter** | Edit selected / submit form |
| **Escape** | Cancel form / close editor |
