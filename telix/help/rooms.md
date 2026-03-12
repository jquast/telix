## Room Browser

**Default key:** Alt + R

The room browser displays a searchable, hierarchical view of all rooms
discovered via GMCP.  Rooms are grouped by name and can be filtered by
area.

### Buttons

| Button | Action |
|--------|--------|
| **Travel** | Fast travel to the selected room |
| **Help** | Open this help screen |
| **Close** | Close the room browser |

### Marker Buttons (bottom bar)

| Button | Action |
|--------|--------|
| **Bookmark ‡** | Toggle a bookmark on the selected room |
| **Block ⌀** | Toggle block -- blocked rooms are excluded from all travel |
| **Home ⌂** | Set as home room for this area (one per area) |
| **Mark ➽** | Toggle a visual marker (no functional effect) |

### Tree View

Rooms are grouped by name.  Parent nodes show the room name with a count
of matching rooms and distance or last-visit info.  Leaf nodes show the
room ID.  A column heading row shows the field layout.

### Area Filter

Use the **Area** dropdown on the left to restrict the tree to a single
area.  Select the blank entry to show all areas.

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| **Enter** | Fast travel to the selected room |
| **\*** | Toggle bookmark on the selected room |
| **B** | Toggle block on the selected room |
| **H** | Toggle home on the selected room |
| **M** | Toggle mark on the selected room |
| **N** | Sort rooms by name |
| **I** | Sort rooms by ID |
| **D** | Sort rooms by distance from current room |
| **L** | Sort rooms by last-visited time |
| **F1** | Open this help screen |
| **Escape** | Close the room browser |

### Search

Type in the search field to filter rooms by name.  The search matches
room names case-insensitively.  Use arrow keys to move between the
search field and the tree.

### Travel

Travel moves through rooms one step at a time.  Triggers fire in
each room along the path.  Use the `noreply` option to disable
triggers during travel.
