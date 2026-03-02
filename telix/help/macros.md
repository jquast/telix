## Macro Editor

Macros bind a **keystroke** to a **command sequence**.  When the key is
pressed during a telnet session, the command text is expanded and executed
exactly as if you had typed it at the input line.

### Table Columns

| Column | Meaning |
|--------|---------|
| **Key** | The keystroke that triggers the macro (e.g. F2, Ctrl+A) |
| **Text** | The command sequence to execute |
| **Last** | Timestamp of the last time this macro was triggered |

### Buttons

| Button | Action |
|--------|--------|
| **Add** | Create a new macro |
| **Edit** | Edit the selected macro |
| **Copy** | Duplicate the selected macro |
| **Delete** | Delete the selected macro (with confirmation) |
| **Save** | Save all changes to disk and close |
| **Cancel** | Discard changes and close |

### Form Fields

- **Enabled** -- toggle the macro on/off without deleting it
- **Key** -- click "Capture" then press the desired keystroke
- **Text** -- the command sequence (use `;` and `|` separators, backtick
  commands)

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| **+** / **=** | Move selected macro up (higher priority) |
| **-** | Move selected macro down |
| **L** | Sort by last-used timestamp |
| **Enter** | Edit selected / submit form |
| **Escape** | Cancel form / close editor |

### Insert Buttons

The form provides insert buttons for common backtick commands.
**Travel** opens the room picker to select a destination.  Others
insert a template you can edit.

### Toggle Macros

A toggle macro alternates between two commands on each press.  Enable
the **Toggle** switch in the form to turn a macro into a toggle.

- **On command** (the "Text" field) -- sent on the first press
- **Off command** -- sent on the second press, then back to On, etc.

Toggle macros always start in the "on" state when the session begins.
The current toggle state is not saved to disk.

### Example Macros

| Key | Text | Notes |
|-----|------|-------|
| Alt+E | `get boots;get jacket;take crysknife;wield crysknife;equip boots;equip cloak` | Equip gear after respawn |
| Alt+H | `` `travel 8bd9a5e5`;5order splint;5order bandage;`return` `` | Buy supplies and return |
| F2 | `` kill bear;`until 10 died\.\|You killed\|Kill what \?`;get all `` | Kill, wait for outcome, loot |
| F4 | `3n;2e;look` | Navigate and look |
| Ctrl+R | `` `return` `` | Return to macro start room |
| F3 | `` `autodiscover 50` `` | Explore 50 unvisited exits |
| F5 | `survey on` / `survey off` | Toggle macro -- alternates each press |
