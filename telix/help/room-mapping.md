## Room Mapping

When the server sends GMCP `Room.Info` messages, telix builds an
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

Press **F7** to open the room browser.  Rooms can be filtered by area,
sorted by name/ID/distance/last-visited, and traveled to directly.

### Autodiscover

Autodiscover explores exits from nearby rooms that lead to unvisited
places.  It travels to each frontier exit, checks the room, then returns
before trying the next branch.

| Option | Meaning |
|--------|---------|
| **BFS** | Explore nearest exits first (breadth-first) |
| **DFS** | Explore farthest exits first (depth-first) |
| **noreply** | Disable autoreply processing during the walk |
| **autosearch** | Send `search` in each new room |
| **autoevaluate** | Send `evaluate` in each new room |

**Warning:** Autodiscover can lead to dangerous areas, death traps, or
aggressive monsters.  Your character may die.

### Random Walk

Random walk explores rooms by picking random exits, preferring unvisited
rooms.  It never returns through the entrance you came from.  Autoreplies
fire in each room.  Stops when all reachable rooms are visited the
required number of times.

| Option | Meaning |
|--------|---------|
| **Visit level** | Minimum visits per room before the walk stops (default 2) |
| **Auto search** | Send `search` in each new room |
| **Auto evaluate** | Send `evaluate` in each new room |
