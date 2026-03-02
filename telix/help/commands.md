## Command Syntax

Commands are separated by **`;`** (wait for server prompt) or **`|`** (send
immediately, no prompt wait).  Whitespace around separators is optional,
including newlines -- so `a;b`, `a ; b`, and `a;\nb` are all equivalent.

A leading number repeats the command that follows it, separated by `;`
(prompt-paced) by default.

| Syntax | Meaning |
|--------|---------|
| `get all;drop sword` | Send "get all", wait for prompt, then "drop sword" |
| `cast heal\|look` | Send "cast heal", then "look" immediately without waiting |
| `3n;2e` | Repeat prefix -- expands to `n;n;n;e;e` |
| `5attack` | Repeat prefix -- expands to `attack;attack;attack;attack;attack` |

### Escaping Separators

Prefix `;`, `|`, or `` ` `` with `\` to include them literally:

| Syntax | Meaning |
|--------|---------|
| `say hey \;)` | Send `say hey ;)` -- the `\;` becomes a literal semicolon |
| `say hmm \|` | Send `say hmm |` -- the `\|` becomes a literal pipe |
| `say \\o/` | Send `say \o/` -- the `\\` becomes a literal backslash |

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

Stop the command chain unless a condition is met.  Conditions check
GMCP vitals first, then fall back to **captured variables** from
highlight rules.

Use **`HP%`** / **`MP%`** for GMCP vital percentages, **`HP`** / **`MP`**
for raw GMCP values, or any captured variable name (e.g.
**`Adrenaline`**, **`Adrenaline%`**).

For percentage conditions on captured variables, the engine looks up
`VariableName` (current) and `MaxVariableName` (max) and computes
the percentage.

| Example | Effect |
|---------|--------|
| `` `when HP%>=80` `` | Continue only if HP is at least 80% of max |
| `` `when MP%>50` `` | Continue only if MP is above 50% of max |
| `` `when HP>=500` `` | Continue only if HP is at least 500 |
| `` `when MP>200` `` | Continue only if MP is above 200 |
| `` `when Adrenaline>100` `` | Continue only if captured Adrenaline > 100 |
| `` `when Adrenaline%>50` `` | Continue only if Adrenaline/MaxAdrenaline > 50% |

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

### Travel

Navigate to a room by its GMCP room ID.  Autoreplies fire in each room
by default; add `noreply` to disable them.

| Example | Effect |
|---------|--------|
| `` `travel abc123` `` | Travel to room abc123 |
| `` `travel abc123 noreply` `` | Travel with autoreplies disabled |

### Return

Travel back to the room where the current macro started executing.

| Example | Effect |
|---------|--------|
| `` `return` `` | Return to start room |
| `` `return noreply` `` | Return with autoreplies disabled |

### Home

Fast travel to the home room of your current area.  Set a home room
in the room browser (F7) using the Home button.

| Example | Effect |
|---------|--------|
| `` `home` `` | Travel to area home room |

### Autodiscover

BFS-explore unvisited exits from nearby rooms.  Optional arguments
(in any order after the verb):

- **limit** -- maximum exits to explore (default 999)
- **bfs** / **dfs** -- search strategy (default bfs)
- **autosearch** -- send ``search`` in each new room
- **autoevaluate** -- enable consider-before-kill autoreply logic
- **autosurvey** -- send ``survey`` in each new room
- **noreply** -- completely disable autoreply processing during the walk

| Example | Effect |
|---------|--------|
| `` `autodiscover` `` | Explore up to 999 unvisited exits |
| `` `autodiscover 50` `` | Explore up to 50 exits |
| `` `autodiscover dfs noreply` `` | DFS explore with autoreplies disabled |
| `` `autodiscover autosearch autosurvey` `` | Explore with auto search and survey |

### Random Walk

Walk randomly, preferring rooms with unvisited exits.  Optional
arguments (in any order after the verb):

- **limit** -- maximum steps (default 999)
- **visit_level** -- minimum visits per room before stopping (default 2)
- **autosearch** -- send ``search`` in each new room
- **autoevaluate** -- enable consider-before-kill autoreply logic
- **autosurvey** -- send ``survey`` in each new room
- **noreply** -- completely disable autoreply processing during the walk

| Example | Effect |
|---------|--------|
| `` `randomwalk` `` | Random walk up to 999 steps |
| `` `randomwalk 100` `` | Random walk up to 100 steps |
| `` `randomwalk noreply autosearch` `` | Walk with autoreplies disabled, auto-search |
| `` `randomwalk autosearch autosurvey` `` | Walk with auto search and survey |

### Resume

Resume the last autodiscover or randomwalk from where it stopped,
carrying over the visited/tried state.  Only works if still in the
same room.  Optional arguments:

- **limit** -- override step limit
- **noreply** -- override the noreply setting (otherwise inherited from original walk)

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
`travel 8bd9a5e5`;5order splint;5order bandage;`return`
```

Travel to a shop, order supplies with repeat prefixes, then return.

```
Dingo;`until 10 Password`
```

Send a name, then wait up to 10s for a password prompt (login sequence).
