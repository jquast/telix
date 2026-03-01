## Highlight Editor

Highlights apply visual styles to server output when a regex pattern
matches.  Rules are evaluated in order; multiple rules can match the
same text.

### Table Columns

| Column | Meaning |
|--------|---------|
| **#** | Priority order |
| **Pattern** | Regex matched against server output |
| **Highlight** | Named style applied to the match |
| **Flags** | **S** = Stop movement, **CS** = Case-sensitive, **M** = Multiline, **C** = Captured |

### Flags Explained

- **S (Stop movement)** — cancel any active autodiscover or randomwalk
  when this pattern matches.  Useful for detecting danger or important
  events during exploration.
- **CS (Case-sensitive)** — match the pattern case-sensitively instead of
  the default case-insensitive matching.
- **M (Multiline)** — match the pattern across multiple lines of output.
  Use `\n` in the pattern to span line boundaries (e.g. `echoes:\n.*hijacked`).
- **C (Captured)** — extract regex groups into named variables and log
  matched lines to a capture channel (see Captures below).

### Form Fields

- **Enabled** — toggle the rule on/off
- **Stop** — cancel movement walks on match
- **Case Sensitive** — case-sensitive matching
- **Multiline** — match pattern across line boundaries
- **Captured** — enable capture extraction (see Captures below)
- **Capture Name** — channel name for the Capture Window (default
  `captures`); may contain group references like `\1`
- **Pattern** — Python regex
- **Highlight** — a style name (see below)
- **Add Capture** — add key/value extraction rows (only visible when
  Captured is on)

### Style Names

Styles are composed from attributes and colors separated by underscores.
Attributes: `bold`, `italic`, `underline`, `blink`, `reverse`.
Colors: `red`, `green`, `yellow`, `blue`, `magenta`, `cyan`, `white`,
`black`.  Prefix with `on_` for background.

| Style | Effect |
|-------|--------|
| `bold_red` | Bold red text |
| `blink_black_on_yellow` | Blinking black text on yellow background |
| `underline_green` | Underlined green text |
| `reverse` | Reversed video |
| `bold_white_on_red` | Bold white text on red background |

### Example Highlights

| Pattern | Style | Flags | Notes |
|---------|-------|-------|-------|
| `^A Level \d+ \w+` | `blink_black_on_yellow` | S | Flash and stop movement for level-gated items |
| `\(medium\)` | `black_on_yellow` | | Highlight medium-difficulty indicators |
| `treasure` | `bold_yellow` | | Highlight loot keywords |
| `\b(gold\|silver)\b` | `bold_white_on_blue` | | Highlight currency |

### Pattern Syntax (Python Regex)

| Pattern | Matches |
|---------|---------|
| `treasure` | Literal text "treasure" |
| `\b(gold\|silver)\b` | Whole word "gold" or "silver" |
| `^You feel` | "You feel" at line start |
| `HP: (\d+)` | "HP:" followed by digits |

### Multiline Matching

When **Multiline** is enabled, the pattern is matched against a full
block of output rather than individual lines.  Use `\n` in the pattern
to span line boundaries.

| Pattern | Matches |
|---------|---------|
| `echoes:\n.*hijacked` | "echoes:" on one line followed by "hijacked" on the next |
| `You feel\n.*faint` | "You feel" followed by "faint" across two lines |

Multiline rules run in a separate pass before per-line highlights.
Single-line rules continue to work unchanged.

### Captures

When **Captured** is enabled on a highlight rule, the full matched line
is logged to a capture channel visible in the **Capture Window** (F10).

**Capture Name** sets the channel.  It can be a fixed name like `tells`
or a group reference like `\1` that resolves dynamically per match.

**Key/Value captures** extract regex groups into named integer variables.
Each row has a **Key** (variable name) and a **Value** (group template
like `\1` or `\2`).  Only integer values are stored; non-numeric
captures are silently skipped.

These variables are available in `when` conditions on autoreplies and
macros.  For example, a highlight on `Adrenaline: (\d+)/(\d+)` with
captures `Adrenaline=\1` and `MaxAdrenaline=\2` lets you write
`` `when Adrenaline%>50` `` in a macro.

| Pattern | Capture Name | Captures | Use case |
|---------|--------------|----------|----------|
| `Adrenaline: (\d+)/(\d+)` | `captures` | Adrenaline=`\1`, MaxAdrenaline=`\2` | Track vitals for `when` conditions |
| `(\w+) tells you:` | `\1` | *(none)* | Log tells per speaker name |
| `^You receive (\d+) xp` | `xp` | XP=`\1` | Track XP gains |

### Capture Window (F10)

Press **F10** to open the Capture Window.  It shows both GMCP chat
messages and highlight capture logs in a unified tabbed view.  Use
**Tab** / **Shift+Tab** to cycle channels.

The `captures` channel displays a key/value table of current captured
variables at the top, followed by captured lines.  Custom channels
show captured lines in a chat-like format with timestamps.

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| **+** / **=** | Move selected rule up (higher priority) |
| **-** | Move selected rule down |
| **Enter** | Edit selected / submit form |
| **Escape** | Cancel form / close editor |
