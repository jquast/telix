## Macro Editor

**Default key:** Alt+M

Macros bind a **keystroke** to a **command sequence**.  When the key is
pressed during a telnet session, the command text is expanded and executed
exactly as if you had typed it at the input line.

### Builtin Macros

Telix ships with builtin macros for all default key bindings (help,
editors, toggles, walk modes, repaint, disconnect).  Builtins are
marked **(builtin)** in the table.  You can rebind the key or
disable a builtin, but you cannot delete it.

### Default Key Bindings

| Key | Action |
|-----|--------|
| F1 | Help |
| F3 | Random walk dialog |
| F4 | Autodiscover dialog |
| F5 | Resume last walk |
| Alt+H | Edit highlights |
| Alt+M | Edit macros |
| Alt+T | Edit triggers |
| Alt+R | Edit rooms |
| Alt+C | Chat viewer / captures |
| Alt+B | Edit bars |
| Alt+E | Edit theme |
| Alt+Shift+H | Toggle highlights |
| Alt+Shift+T | Toggle triggers |
| Alt+Q | Stop all running scripts |
| Ctrl+L | Repaint screen |
| Ctrl+] | Disconnect |

### Table Columns

| Column | Meaning |
|--------|---------|
| **Key** | The keystroke that triggers the macro (e.g. F2, Ctrl+A) |
| **Command Text** | The command sequence to execute |
| **Last** | Timestamp of the last time this macro was triggered |

### Buttons

| Button | Action |
|--------|--------|
| **Add** | Create a new macro |
| **Edit** | Edit the selected macro |
| **Copy** | Duplicate the selected macro |
| **Delete** | Delete the selected macro (builtins cannot be deleted) |
| **Save** | Save all changes to disk and close |
| **Cancel** | Discard changes and close |

### Form Fields

- **Enabled** -- toggle the macro on/off without deleting it
- **Key** -- click "Capture" then press the desired keystroke
- **Command Text** -- the command sequence (use `;` and `|` separators, backtick
  commands); read-only for builtin macros

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

- **On command** (the "Command Text" field) -- sent on the first press
- **Off command** -- sent on the second press, then back to On, etc.

Toggle macros always start in the "on" state when the session begins.
The current toggle state is not saved to disk.

### Example User Macros

| Key | Text | Notes |
|-----|------|-------|
| Alt + G | `get boots;get jacket;take crysknife;wield crysknife;equip boots;equip cloak` | Equip gear after respawn |
| Alt + S | `` `travel 8bd9a5e5`;5order splint;5order bandage;`return` `` | Heal at supply store and return |
| ALT + B | `` kill bear;`until 10 died\.\|You killed\|Kill what \?`;get all `` | Kill, wait for outcome, loot |
| Alt + D | `` `autodiscover 1` `` | Explore one unvisited exit |
