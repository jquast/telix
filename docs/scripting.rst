=========
Scripting
=========

Telix offers asyncio Python scripts for complex MUD automation.

This is mainly to take advantage of :func:`asyncio.wait` to await *multiple*
conditions and to more naturally handle timing.

Quickstart
----------

Create a file named ``demo.py`` in the current directory where ``telix`` is launched, or in
``~/.config/telix/scripts/`` to discover it from the REPL::

    async def run(ctx):
        room = ctx.room
        ctx.print(f"[demo] You are in: {room.name} ({room.area})")
        exits = ctx.neighbors()
        ctx.print(f"[demo] Exits: {', '.join(exits)}")

Then in the Telix REPL, execute command::

    `script demo`

This should produce example output::

    [demo] You are in: Mayor's office (caladan)
    [demo] Exits: west


The script runs as a background async task.  Use `` `stopscript` `` to cancel it.

Script search path
------------------

Telix searches for scripts in this order:

1. Current working directory (``os.getcwd()``) -- launch telix in your project
   folder to use scripts without any configuration.
2. ``~/.config/telix/scripts/`` -- persistent user-global scripts.

Scripts can import sibling modules naturally because the search directories are
temporarily added to ``sys.path`` during load.

Triggering scripts
------------------

Scripts can be triggered from any command surface that supports backtick commands:

- **REPL**: type `` `script module.fn` `` at the prompt.
- **Autoreply reply field**: set the reply to `` `script module.fn` ``.
- **Macro text**: include `` `script module.fn` `` in macro text.
- **Chained command**: `` look;`script combat.hunt goblin`;north ``

The ``ctx`` object
------------------

Every script receives a ``ctx`` argument of type :class:`~telix.scripts.ScriptContext`.

Sending commands
~~~~~~~~~~~~~~~~

``await ctx.send(line)``
    Send a command string with full expansion.  The same syntax available
    at the REPL is supported:

    **Separators**

    - ``;`` -- send a command and wait for the next server prompt before continuing
    - ``|`` -- send a command immediately without waiting for a prompt
    - Leading number repeats the command: ``3n`` expands to ``n;n;n``

    **Backtick directives** (processed by the client, not sent to the server)

    ============================== ================================================
    Directive                      Effect
    ============================== ================================================
    `` `delay 1s` ``               Pause 1 second (also ``500ms``, ``0.5s``)
    `` `when HP%>=80` ``           Stop the chain unless condition is met.
                                   Keys: ``HP%``, ``MP%``, ``HP``, ``MP``, or any
                                   captured variable.  Ops: ``>=``, ``<=``, ``>``,
                                   ``<``, ``=``
    `` `until died\.` ``           Wait up to 4 s (default) for a regex pattern.
    `` `until 10 pattern` ``       Wait up to 10 s.  Pattern is case-insensitive.
    `` `untils 2 DEAD` ``          Same as ``until`` but case-sensitive.
    `` `travel abc123` ``          Navigate to a room by GMCP room ID.
    `` `travel abc123 noreply` ``  Travel with autoreplies disabled.
    `` `return` ``                 Return to the room where the current macro started.
    `` `home` ``                   Fast-travel to the home room of the current area.
    `` `autodiscover` ``           BFS-explore unvisited exits (add ``limit N``,
                                   ``dfs``, ``autosearch``, ``noreply``, etc.).
    `` `randomwalk` ``             Random walk preferring unvisited exits (same
                                   optional args as ``autodiscover``).
    `` `resume` ``                 Resume the last autodiscover/randomwalk.
    `` `script module.fn` ``       Start a script as a background task.
    `` `stopscript` ``             Cancel all running scripts.
    ============================== ================================================

    Full command syntax and more examples: :ref:`user-manual:command syntax`.

Prompt waiting
~~~~~~~~~~~~~~

``await ctx.prompt(timeout=30.0)``
    Wait for one GA/EOR prompt signal.  Returns ``True`` if it arrived.

``await ctx.prompts(n, timeout=30.0)``
    Wait for *n* consecutive prompts.  Returns ``True`` if all arrived.

Output buffering
~~~~~~~~~~~~~~~~

``ctx.output(clear=False)``
    Return all buffered server output since the script started (or since the
    last ``clear=True`` call).

``ctx.turns(n=5)``
    Return the last *n* prompt-delimited output blocks as a list of strings.

Pattern matching
~~~~~~~~~~~~~~~~

``await ctx.wait_for(pattern, timeout=30.0)``
    Wait for *pattern* (a regex string) to appear in server output.
    Returns a :class:`re.Match` object, or ``None`` on timeout.

Condition polling
~~~~~~~~~~~~~~~~~

``await ctx.condition_met(key, op, threshold, poll_interval=0.25)``
    Poll a GMCP or capture condition until it becomes true.  This coroutine is
    cancellable so it works with :func:`asyncio.wait`.

    *key* can be ``"HP%"``, ``"MP%"``, ``"HP"``, ``"MP"``, or any captured
    variable name.  *op* is one of ``">"``, ``"<"``, ``">="``, ``"<="``,
    ``"="``.

Terminal output
~~~~~~~~~~~~~~~

``ctx.print(text)``
    Write *text* to the terminal scroll region in cyan, using the same
    mechanism as autoreply and travel notifications.

``ctx.log(msg)``
    Write *msg* to the telix log at INFO level.

GMCP data
~~~~~~~~~

``ctx.gmcp``
    The full GMCP data dict (same as ``ctx.gmcp_data`` in session context).

``ctx.gmcp_get(dotted_path)``
    Retrieve a nested GMCP value by dot-separated path, e.g.
    ``ctx.gmcp_get("Char.Vitals.hp")``.  Returns ``None`` if not found.

Room graph
~~~~~~~~~~

``ctx.room_id``
    Current room number string.

``ctx.room``
    Current :class:`~telix.rooms.Room` object, or ``None`` if unknown.

``ctx.room_graph``
    The :class:`~telix.rooms.RoomStore` for this session.

``ctx.get_room(num)``
    Look up a room by number string.

``ctx.neighbors()``
    Return ``{direction: room_num}`` for exits from the current room.

``ctx.find_path(dst)``
    Find a path from the current room to *dst* (room number string).
    Returns a list of direction strings, or ``None`` if unreachable.

``ctx.captures``
    Highlight capture variable dict.

Arguments
---------

Everything after the module.function token is split (shell-style) and passed
as positional ``*args``::

    `script rooms.goto 12345`
    `script combat.hunt goblin "dark lair"`

In the script::

    async def goto(ctx, *args):
        room_id = args[0] if args else ""
        ...

    async def hunt(ctx, *args):
        target = args[0] if args else "goblin"
        place = args[1] if len(args) > 1 else ""
        ...

Module / function naming
------------------------

The last dot-separated segment of the first token is the function name;
everything before it is the importable module path:

- `` `script demo` `` -- imports ``demo``, calls ``run(ctx)``
- `` `script combat.hunt` `` -- imports ``combat``, calls ``hunt(ctx)``

Stopping scripts
----------------

`` `stopscript` ``
    Cancel all running scripts.  Each cancelled script name is echoed to the
    terminal: ``[stopscript] stopped: combat.hunt``.  Nothing is printed if no
    scripts were running.

`` `stopscript combat.hunt` ``
    Cancel only the named script (same feedback line).

Chaining scripts
----------------

`` `script hunt` `` is itself a backtick command, so ``ctx.send`` can launch
a script from inside another script::

    await ctx.send("`script hunt`")

This starts ``hunt`` as a new background task and returns immediately -- it does
**not** wait for the ``hunt`` to finish.  Both scripts run concurrently!

To run scripts sequentially, import and call the function directly::

    import combat
    await combat.hunt(ctx, "goblin")   # waits for hunt to finish

Multi-condition waits
---------------------

Use :func:`asyncio.wait` with ``FIRST_COMPLETED`` to react to whichever
condition fires first::

    import asyncio

    async def hunt(ctx, *args):
        target = args[0] if args else "goblin"
        await ctx.send(f"kill {target}")
        done, pending = await asyncio.wait(
            [
                asyncio.ensure_future(ctx.wait_for(f"{target} has died")),
                asyncio.ensure_future(ctx.wait_for("You flee")),
                asyncio.ensure_future(ctx.condition_met("HP%", "<", 25)),
            ],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        ctx.print("[hunt] done")

Complete examples
-----------------

Room info script::

    async def run(ctx):
        room = ctx.room
        if room is None:
            ctx.print("[demo] No room data")
            return
        ctx.print(f"[demo] Room: {room.name} ({room.area})")
        ctx.print(f"[demo] Exits: {', '.join(ctx.neighbors())}")

Travel to a room by number::

    async def goto(ctx, *args):
        if not args:
            ctx.print("[rooms] Usage: `script rooms.goto <room_id>`")
            return
        path = ctx.find_path(args[0])
        if path is None:
            ctx.print(f"[rooms] No path to {args[0]}")
            return
        await ctx.send(";".join(path))

Wait for a pattern then react::

    async def hunt(ctx, *args):
        target = args[0] if args else "goblin"
        await ctx.send(f"consider {target}")
        m = await ctx.wait_for(r"seems? .* formidable|is? no match", timeout=5.0)
        if not m or "no match" not in m.group(0):
            ctx.print(f"[hunt] {target} too tough, skipping")
            return
        await ctx.send(f"kill {target}")
        ctx.print("[hunt] fighting!")
