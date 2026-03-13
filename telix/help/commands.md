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

Stop the command chain unless a condition is met.  The key is the
actual GMCP field name (case-sensitive) and is searched across all
GMCP packages.  Falls back to **captured variables** from highlight
rules if not found in GMCP data.

Append ``%`` to compute a percentage from the field and its ``Max``
counterpart.  Operators: ``>``, ``<``, ``>=``, ``<=``, ``=``, ``!=``.
String comparisons work with ``=`` and ``!=``.

| Example | Effect |
|---------|--------|
| `` `when hp%>=80` `` | Continue only if hp is at least 80% of maxhp |
| `` `when mp%>50` `` | Continue only if mp is above 50% of maxmp |
| `` `when hp>=500` `` | Continue only if hp is at least 500 |
| `` `when mp>200` `` | Continue only if mp is above 200 |
| `` `when Mode!=Rage` `` | Continue only if Mode is not Rage |
| `` `when Adrenaline>100` `` | Continue only if captured Adrenaline > 100 |
| `` `when Adrenaline%>50` `` | Continue only if Adrenaline/MaxAdrenaline > 50% |

Operators: `>`, `<`, `>=`, `<=`, `=`, `!=`

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

Navigate to a room by its GMCP room ID.  Triggers fire in each room
by default; add `noreply` to disable them.

| Example | Effect |
|---------|--------|
| `` `travel abc123` `` | Travel to room abc123 |
| `` `travel abc123 noreply` `` | Travel with triggers disabled |

### Return

Travel back to the room where the current macro started executing.

| Example | Effect |
|---------|--------|
| `` `return` `` | Return to start room |
| `` `return noreply` `` | Return with triggers disabled |

### Home

Fast travel to the home room of your current area.  Set a home room
in the room browser (Alt + R) using the Home button.

| Example | Effect |
|---------|--------|
| `` `home` `` | Travel to area home room |

### Autodiscover

BFS-explore unvisited exits from nearby rooms.  Optional arguments
(in any order after the verb, except ``roomcmd`` which must be last):

- **limit** -- maximum exits to explore (default 999)
- **bfs** / **dfs** -- search strategy (default bfs)
- **noreply** -- completely disable trigger processing during the walk
- **delay** -- seconds between movement commands (default 0.25); increase
  for servers that limit movement speed (e.g. breath or fatigue systems)
- **roomcmd** *cmds* -- commands to run in each new room; everything after
  ``roomcmd`` is the command string, processed through the full client pipeline
  (semicolons wait for prompt, ``|`` sends immediately, backtick commands run
  locally -- escape backticks inside the string as ``\```)

| Example | Effect |
|---------|--------|
| `` `autodiscover` `` | Explore up to 999 unvisited exits |
| `` `autodiscover 50` `` | Explore up to 50 exits |
| `` `autodiscover dfs noreply` `` | DFS explore with triggers disabled |
| `` `autodiscover roomcmd search;survey` `` | Explore, running search and survey in each room |
| `` `autodiscover noreply roomcmd \`async hunt\`` `` | Explore with triggers off, running a hunt script |

### Random Walk

Walk randomly, preferring rooms with unvisited exits.  Optional
arguments (in any order after the verb, except ``roomcmd`` which must be last):

- **limit** -- maximum steps (default 999)
- **visit_level** -- minimum visits per room before stopping (default 2)
- **noreply** -- completely disable trigger processing during the walk
- **delay** -- seconds between movement commands (default 0.25); increase
  for servers that limit movement speed (e.g. breath or fatigue systems)
- **roomcmd** *cmds* -- commands to run in each new room; everything after
  ``roomcmd`` is the command string, processed through the full client pipeline
  (semicolons wait for prompt, ``|`` sends immediately, backtick commands run
  locally -- escape backticks inside the string as ``\```)

| Example | Effect |
|---------|--------|
| `` `randomwalk` `` | Random walk up to 999 steps |
| `` `randomwalk 100` `` | Random walk up to 100 steps |
| `` `randomwalk noreply` `` | Walk with triggers disabled |
| `` `randomwalk roomcmd search;survey` `` | Walk, running search and survey in each room |
| `` `randomwalk noreply roomcmd search;survey;\`async hunt\`` `` | Walk with triggers off and full room sweep |

### Script

Run an async Python script.  Scripts are searched in the current directory
and ``~/.config/telix/scripts/``.

`` `async NAME` `` fires the script in the background and returns immediately;
`` `await NAME` `` runs the script and waits for it to finish before
continuing -- useful inside ``roomcmd`` sequences where you need the script to
complete before the walk moves on.

**Warning:** Scripts run concurrently and asynchronously.  Multiple scripts
(or a script combined with active triggers) can send commands simultaneously,
potentially flooding the server with rapid input -- a "server storm".  Use
`` `stopscript` `` or press **Alt + Q** to stop all running scripts immediately.

| Example | Effect |
|---------|--------|
| `` `async NAME` `` | Fire-and-forget async Python script NAME |
| `` `async MODULE.FUNC` `` | Run a specific named async function from MODULE |
| `` `await NAME` `` | Run script NAME and block until it finishes |
| `` `await MODULE.FUNC arg` `` | Run a specific function and wait for completion |
| `` `scripts` `` | List all currently running scripts |
| `` `stopscript` `` | Stop all running scripts |
| `` `stopscript NAME` `` | Stop the named script |

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
