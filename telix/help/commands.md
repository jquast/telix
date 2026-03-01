## Command Syntax

Commands are separated by **`;`** (wait for server prompt) or **`|`** (send
immediately, no prompt wait).  Whitespace around separators is optional,
including newlines — so `a;b`, `a ; b`, and `a;\nb` are all equivalent.

A leading number repeats the command that follows it, separated by `;`
(prompt-paced) by default.

| Syntax | Meaning |
|--------|---------|
| `get all;drop sword` | Send "get all", wait for prompt, then "drop sword" |
| `cast heal\|look` | Send "cast heal", then "look" immediately without waiting |
| `3n;2e` | Repeat prefix — expands to `n;n;n;e;e` |
| `5attack` | Repeat prefix — expands to `attack;attack;attack;attack;attack` |

### Escaping Separators

Prefix `;`, `|`, or `` ` `` with `\` to include them literally:

| Syntax | Meaning |
|--------|---------|
| `say hey \;)` | Send `say hey ;)` — the `\;` becomes a literal semicolon |
| `say hmm \|` | Send `say hmm |` — the `\|` becomes a literal pipe |
| `say \\o/` | Send `say \o/` — the `\\` becomes a literal backslash |

## Backtick Commands

Backtick-enclosed commands are special directives processed by the client.
They are **not** split on `;` or `|` internally.

### Delay

Pause execution for a duration.

| Example | Effect |
|---------|--------|
| `` `delay 1s` `` | Pause 1 second |
| `` `delay 500ms` `` | Pause 500 milliseconds |
| `` `delay 0.5s` `` | Pause 0.5 seconds |

### When (Condition Gate)

Stop the command chain unless a GMCP vital condition is met.
Use **`HP%`** / **`MP%`** for percentages of max, or **`HP`** / **`MP`**
for raw values.

| Example | Effect |
|---------|--------|
| `` `when HP%>=80` `` | Continue only if HP is at least 80% of max |
| `` `when MP%>50` `` | Continue only if MP is above 50% of max |
| `` `when HP>=500` `` | Continue only if HP is at least 500 |
| `` `when MP>200` `` | Continue only if MP is above 200 |

Operators: `>=`, `<=`, `>`, `<`, `=`

### Until (Wait for Pattern)

Pause the chain until a regex pattern appears in server output,
or a timeout expires (default 4 seconds). **Case-insensitive.**
The pattern is a regex, so `|` inside the pattern means alternation.

| Example | Effect |
|---------|--------|
| `` `until died\.` `` | Wait up to 4s for "died." |
| `` `until 10 died\.` `` | Wait up to 10s for "died." |
| `` `until 2 treasure` `` | Wait up to 2s for "treasure" |
| `` `until 10 died\.\|You killed\|Kill what \?` `` | Wait for kill, miss, or error |

### Untils (Case-Sensitive Until)

Same as `until` but the pattern match is **case-sensitive**.

| Example | Effect |
|---------|--------|
| `` `untils 2 DEAD` `` | Wait up to 2s for exactly "DEAD" |

### Fast Travel / Slow Travel

Navigate to a room by its GMCP room ID.  Fast travel suppresses exclusive
autoreplies (e.g. combat triggers); slow travel allows them to fire.

| Example | Effect |
|---------|--------|
| `` `fast travel abc123` `` | Fast travel to room abc123 |
| `` `slow travel abc123` `` | Slow travel (autoreplies fire) |

### Return Fast / Return Slow

Travel back to the room where the current macro started executing.

| Example | Effect |
|---------|--------|
| `` `return fast` `` | Return to start room (fast) |
| `` `return slow` `` | Return to start room (slow, autoreplies fire) |

### Autodiscover

BFS-explore unvisited exits from nearby rooms.  Optional arguments
(in any order after the verb):

- **limit** — maximum exits to explore (default 999)
- **bfs** / **dfs** — search strategy (default bfs)
- **autosearch** — send ``search`` in each new room
- **autoevaluate** — send ``evaluate`` in each new room
- **noreply** — completely disable autoreply processing during the walk

| Example | Effect |
|---------|--------|
| `` `autodiscover` `` | Explore up to 999 unvisited exits |
| `` `autodiscover 50` `` | Explore up to 50 exits |
| `` `autodiscover dfs noreply` `` | DFS explore with autoreplies disabled |

### Random Walk

Walk randomly, preferring rooms with unvisited exits.  Optional
arguments (in any order after the verb):

- **limit** — maximum steps (default 999)
- **visit_level** — minimum visits per room before stopping (default 2)
- **bfs** / **dfs** — search strategy (default bfs)
- **autosearch** — send ``search`` in each new room
- **autoevaluate** — send ``evaluate`` in each new room
- **noreply** — completely disable autoreply processing during the walk

| Example | Effect |
|---------|--------|
| `` `randomwalk` `` | Random walk up to 999 steps |
| `` `randomwalk 100` `` | Random walk up to 100 steps |
| `` `randomwalk noreply autosearch` `` | Walk with autoreplies disabled, auto-search |

### Resume

Resume the last autodiscover or randomwalk from where it stopped,
carrying over the visited/tried state.  Only works if still in the
same room.  Optional arguments:

- **limit** — override step limit
- **noreply** — override the noreply setting (otherwise inherited from original walk)

| Example | Effect |
|---------|--------|
| `` `resume` `` | Resume last walk mode |
| `` `resume 200` `` | Resume with a 200-step limit |

## Combining Commands

Commands, backtick directives, repeat prefixes, and separators can be
freely mixed:

```
kill bear;`until 10 died\.\|You killed\|Kill what \?`;get all
```

Kill a bear, wait for it to die (or detect a miss), then loot.

```
`fast travel 8bd9a5e5`;5order splint;5order bandage;`return slow`
```

Fast-travel to a shop, order supplies with repeat prefixes, then return
via slow travel so autoreplies fire on the way back.

```
Dingo;`until 10 Password`
```

Send a name, then wait up to 10s for a password prompt (login sequence).
