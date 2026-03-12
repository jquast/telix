## Room Mapping

**Keys:** F3 Random walk -- F4 Autodiscover -- F5 Resume -- Alt + R Room browser

When the server sends GMCP `Room.Info` messages, Telix builds an
incrementally-growing room graph stored in SQLite at
`$XDG_DATA_HOME/telix/rooms-<hash>.db`.

The room graph supports:

- BFS shortest-path navigation (travel)
- Autodiscover (BFS-explore unvisited exits)
- Random walk (prefer rooms with unvisited exits)
- Room markers: bookmarks, blocks (excluded from pathfinding), home (one per
  area), and visual marks
- Blocked exits to prevent travel through dangerous areas
- ID rotation detection for rooms that change hash each visit

### Room Browser (Alt + R)

Press **Alt + R** to open the room browser.  Rooms can be filtered by area,
sorted by name/ID/distance/last-visited, and traveled to directly.

Room markers:

| Marker | Symbol | Meaning |
|--------|--------|---------|
| **Home** | ⌂ | Home room for an area (one per area) |
| **Block** | ⌀ | Excluded from all pathfinding |
| **Mark** | ➽ | Visual marker (no functional effect) |
| **Bookmark** | ‡ | Favourite room (sorted first in search results) |

### Autodiscover (F4)

**Default key:** F4 — **Command:** `` `autodiscover [limit] [bfs|dfs] [options]` ``

Autodiscover explores exits from nearby rooms that lead to unvisited
places.  It travels to each frontier exit, checks the room, then returns
before trying the next branch.

| Option | Meaning |
|--------|---------|
| **limit** | Maximum number of exits to explore (default 999) |
| **BFS** | Explore nearest exits first (breadth-first, default) |
| **DFS** | Explore farthest exits first (depth-first) |
| **noreply** | Disable trigger processing during the walk |
| **roomcmd** | Commands to execute in each newly discovered room |

**Warning:** Autodiscover can lead to dangerous areas, death traps, or
aggressive monsters.  Your character may die.

### Random Walk (F3)

**Default key:** F3 — **Command:** `` `randomwalk [limit] [visit_level] [options]` ``

Random walk explores rooms by picking random exits, preferring unvisited
rooms.  It never returns through the entrance you came from.  Triggers
fire in each room.  Stops when all reachable rooms are visited the
required number of times.

| Option | Meaning |
|--------|---------|
| **limit** | Maximum number of steps (default 999) |
| **visit_level** | Minimum visits per room before stopping (default 2) |
| **noreply** | Disable trigger processing during the walk |
| **roomcmd** | Commands to execute in each newly discovered room |

### Resume (F5)

**Default key:** F5 — **Command:** `` `resume [limit] [noreply]` ``

Resume the last autodiscover or random walk from where it stopped,
carrying over the visited/tried state.  Only works if still in the
same room.

### Travel

**Command:** `` `travel <room_id> [noreply]` ``

Travel to a specific room by ID using BFS shortest-path.  Triggers
fire in each room along the path unless `noreply` is given.

Use `` `return [noreply]` `` to travel back to the room where the
current macro started, or `` `home` `` to travel to the home room of
the current area.
