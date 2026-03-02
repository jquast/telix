## Room Mapping

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

### Room Browser (F7)

Press **F7** to open the room browser.  Rooms can be filtered by area,
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
| **BFS** | Explore nearest exits first (breadth-first) |
| **DFS** | Explore farthest exits first (depth-first) |
| **noreply** | Disable autoreply processing during the walk |
| **autosearch** | Send `search` in each new room |
| **autoevaluate** | Enable consider-before-kill autoreply logic |
| **autosurvey** | Send `survey` in each new room |

**Warning:** Autodiscover can lead to dangerous areas, death traps, or
aggressive monsters.  Your character may die.

### Random Walk (F3)

**Default key:** F3 — **Command:** `` `randomwalk [limit] [visit_level] [options]` ``

Random walk explores rooms by picking random exits, preferring unvisited
rooms.  It never returns through the entrance you came from.  Autoreplies
fire in each room.  Stops when all reachable rooms are visited the
required number of times.

| Option | Meaning |
|--------|---------|
| **Visit level** | Minimum visits per room before the walk stops (default 2) |
| **autosearch** | Send `search` in each new room |
| **autoevaluate** | Enable consider-before-kill autoreply logic |
| **autosurvey** | Send `survey` in each new room |
| **noreply** | Disable autoreply processing during the walk |

### Resume (F5)

**Default key:** F5 — **Command:** `` `resume [limit] [noreply]` ``

Resume the last autodiscover or random walk from where it stopped,
carrying over the visited/tried state.  Only works if still in the
same room.

### Travel

**Command:** `` `travel <room_id> [noreply]` ``

Travel to a specific room by ID using BFS shortest-path.  Autoreplies
fire in each room along the path unless `noreply` is given.

Use `` `return [noreply]` `` to travel back to the room where the
current macro started, or `` `home` `` to travel to the home room of
the current area.
