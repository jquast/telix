=========
Scripting
=========

Telix scripts are Python files that send commands, wait for responses, and
react to what the server says -- all in plain Python.  Scripts are good for
anything too complex or too repetitive to do by hand: hunting loops, healing
routines, mapping runs, or any sequence that depends on timing or server output.

Quickstart
----------

Create a file named ``demo.py`` in the current directory where ``telix`` is
launched, or in ``~/.config/telix/scripts/``::

    from telix.scripts import ScriptContext

    async def run(ctx: ScriptContext) -> None:
        room = ctx.room
        ctx.print(f"[demo] You are in: {room.name} ({room.area})")
        exits = ctx.neighbors()
        ctx.print(f"[demo] Exits: {', '.join(exits)}")

Then in the Telix REPL, run it::

    `script demo`

Example output::

    [demo] You are in: Mayor's office (caladan)
    [demo] Exits: west


Triggering scripts
------------------

Scripts can be started from anywhere you can type a command:

- **REPL**: type ```script module.fn``` at the prompt.
- **Trigger reply field**: set the reply to ```script module.fn```.
- **Macro text**: include ```script module.fn``` in macro text.
- **Chained command**: `` look;`script combat.hunt goblin`;north ``

Lifecycle
---------

Each script runs in the background while you continue playing.  Multiple
scripts can be running at the same time -- a healer, a combat loop, and a room
tracker all at once.  Because they all share the same connection, it is easy to
accidentally flood the server with conflicting commands.  Use the commands below
to keep track of what is running.  To stop everything immediately, press
**Alt+Q**.

Listing scripts
~~~~~~~~~~~~~~~

```scripts```
    Show the names of all currently running scripts::

        [scripts] running: combat.hunt
        [scripts] running: healer.top_up

    If nothing is running: ``[scripts] no scripts running``.

Stopping scripts
~~~~~~~~~~~~~~~~

```stopscript```
    Stop all running scripts.  Each name is printed as it stops:
    ``[stopscript] stopped: combat.hunt``.

```stopscript combat.hunt```
    Stop only the named script.

Chaining scripts
~~~~~~~~~~~~~~~~

You can start a script from inside another script using ``ctx.send``::

    await ctx.send("`script hunt`")

This starts ``hunt`` in the background and returns immediately -- both scripts
then run at the same time.

To run scripts one after another instead, import and call the function
directly::

    import combat
    await combat.hunt(ctx, "goblin")   # waits for hunt to finish

Reloading
~~~~~~~~~

Every time a script is started, telix checks whether the source file has
changed.  If it has, the module is reloaded automatically before running.
Edit the file, run the script again -- no restart needed.

Search path
~~~~~~~~~~~

Telix looks for scripts in this order:

1. Current working directory -- launch telix from your project folder and
   scripts there are found automatically.
2. ``~/.config/telix/scripts/`` -- a good place for scripts you want
   available everywhere.

The last part of the script name is the function to call; everything before
it is the file to load:

- ```script demo``` -- loads ``demo.py``, calls ``run(ctx)``
- ```script combat.hunt``` -- loads ``combat.py``, calls ``hunt(ctx)``

IDE support and type checking
-----------------------------

Adding a type annotation to ``ctx`` gives your editor autocomplete for every
``ctx.`` method, and lets tools like mypy or Pylance catch mistakes before you
run the script -- including the easy-to-miss error of forgetting ``await`` on
an async call::

    from __future__ import annotations
    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from telix.scripts import ScriptContext

    async def run(ctx: ScriptContext) -> None:
        ctx.send("look")          # Pylance/mypy: error -- missing await
        await ctx.send("look")    # correct

The ``ctx`` object
------------------

Every script receives a ``ctx`` argument -- your handle on everything telix
knows about the current session.

Sending commands
~~~~~~~~~~~~~~~~

``await ctx.send(line)``
    Send a command to the server.  Supports the same syntax as the REPL:

    - ``;`` between commands waits for the server's prompt before sending the next
    - ``|`` sends immediately without waiting
    - A leading number repeats: ``3n`` sends ``n`` three times

    Backtick directives like ```script``` and ```until``` are handled by the
    client, not sent to the server.  See :ref:`backtick commands` for the full
    list.

Prompt waiting
~~~~~~~~~~~~~~

``await ctx.prompt(timeout=30.0)``
    Wait for the server's next prompt.  Returns ``True`` if it arrived within
    the timeout, ``False`` otherwise.

``await ctx.prompts(n, timeout=30.0)``
    Wait for *n* prompts in a row.  Useful for pacing a sequence of commands.

Output buffering
~~~~~~~~~~~~~~~~

``ctx.output(clear=True)``
    Return everything the server has sent since the script started, as a single
    string.  The buffer is cleared afterwards by default, so the next call only
    shows new output.  Pass ``clear=False`` to read without clearing.

``ctx.turns(n=5)``
    Return the last *n* chunks of server output, split at each prompt.  Useful
    when you want to inspect the response to a specific command.

Pattern matching
~~~~~~~~~~~~~~~~

``await ctx.wait_for(pattern, timeout=30.0)``
    Wait for a line of server output matching *pattern* (a regular expression).
    Returns the match object when found, or ``None`` on timeout.

Condition polling
~~~~~~~~~~~~~~~~~

``await ctx.condition_met(key, op, threshold, poll_interval=0.25)``
    Wait until a numeric condition becomes true, checking every
    *poll_interval* seconds.

    *key* can be ``"HP%"``, ``"MP%"``, ``"HP"``, ``"MP"``, or the name of any
    highlight capture variable.  *op* is one of ``">"``, ``"<"``, ``">="``,
    ``"<="``, ``"="``.

    Works well inside :func:`asyncio.wait` -- see `Multi-condition waits`_ below.

Terminal output
~~~~~~~~~~~~~~~

``ctx.print(*args, sep=" ")``
    Print a message to the terminal in cyan.  Works like Python's built-in
    ``print``: pass multiple values and they are joined with *sep*.

``ctx.log(msg)``
    Write a message to the telix log file at INFO level.

GMCP data
~~~~~~~~~

``ctx.gmcp``
    The full GMCP data dictionary, as received from the server.

``ctx.gmcp_get(dotted_path)``
    Read a value out of the GMCP data by path, e.g.
    ``ctx.gmcp_get("Char.Vitals.hp")``.  Returns ``None`` if not found.

Room graph
~~~~~~~~~~

``ctx.room_id``
    The current room's number, as a string.

``ctx.previous_room_id``
    The number of the room you were in before the current one.

``ctx.room``
    The current :class:`~telix.rooms.Room` object (name, area, exits), or
    ``None`` if telix does not yet know what room you are in.

``ctx.room_graph``
    The full :class:`~telix.rooms.RoomStore` -- all rooms telix has mapped for
    this session.

``ctx.get_room(num)``
    Look up any room by its number.

``ctx.neighbors()``
    Return the exits from the current room as ``{direction: room_num}``.

``ctx.find_path(dst)``
    Find directions from the current room to *dst*.  Returns a list of
    direction strings, or ``None`` if no route is known.

``await ctx.room_changed(timeout=30.0)``
    Wait until you move to a new room.  Returns ``True`` on a room change,
    ``False`` on timeout::

        async def tracker(ctx: ScriptContext) -> None:
            while True:
                if not await ctx.room_changed(timeout=60.0):
                    break
                ctx.print(f"[tracker] {ctx.previous_room_id} -> {ctx.room_id}")

``ctx.captures``
    The current value of each highlight capture variable, as a dictionary.

``ctx.capture_log``
    The full history of every capture event -- useful for tracking how a value
    has changed over time, e.g. HP across several combat rounds.

Session identity
~~~~~~~~~~~~~~~~

``ctx.session_key``
    A string identifying the current connection, in ``"host:port"`` form.
    Useful when a script needs to save data per server.

Chat
~~~~

``ctx.chat_messages``
    All chat and tell messages received this session.

``ctx.chat_unread``
    How many messages have arrived since the last time they were read.

``ctx.chat_channels``
    The list of available chat channels.

Walk control
~~~~~~~~~~~~

``ctx.walk_active``
    ``True`` if autodiscover, randomwalk, or travel is currently running.

``ctx.stop_walk()``
    Stop any active walk.  Call this before sending your own movement commands
    to avoid conflicts::

        async def scout(ctx: ScriptContext, *args: str) -> None:
            if ctx.walk_active:
                ctx.stop_walk()
                await ctx.prompt()
            await ctx.send("look")

Arguments
---------

Anything you type after the script name is passed to the function as
positional string arguments::

    `script rooms.goto 12345`
    `script combat.hunt goblin "dark lair"`

In the script, receive them via ``*args``::

    async def goto(ctx: ScriptContext, *args: str) -> None:
        room_id = args[0] if args else ""
        ...

    async def hunt(ctx: ScriptContext, *args: str) -> None:
        target = args[0] if args else "goblin"
        place = args[1] if len(args) > 1 else ""
        ...

Multi-condition waits
---------------------

Sometimes you want to react to whichever thing happens first -- the enemy
dies, you flee, or your HP drops too low.  :func:`asyncio.wait` with
``FIRST_COMPLETED`` handles this: give it a list of things to watch for, and
it returns as soon as one of them fires::

    import asyncio
    from telix.scripts import ScriptContext

    async def hunt(ctx: ScriptContext, *args: str) -> None:
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

Shared state
------------

Scripts that run simultaneously can share data by keeping it in a separate
module.  Any script that imports that module gets the same object, so changes
made by one script are immediately visible to another.

For example, create ``state.py``::

    monsters: list[str] = []
    kills: int = 0

Then any number of scripts can read and write it::

    import state

    async def run(ctx: ScriptContext) -> None:
        state.monsters.append("goblin")
        ctx.print(f"kills so far: {state.kills}")

One thing to be aware of: if you edit a file and run it again, telix reloads
it automatically.  Any variables defined at the top of that file go back to
their initial values, so accumulated data is lost.  If you need data to
survive a reload, keep it in a file or database instead.

Complete examples
-----------------

Room info script::

    from telix.scripts import ScriptContext

    async def run(ctx: ScriptContext) -> None:
        room = ctx.room
        if room is None:
            ctx.print("[demo] No room data")
            return
        ctx.print(f"[demo] Room: {room.name} ({room.area})")
        ctx.print(f"[demo] Exits: {', '.join(ctx.neighbors())}")

Travel to a room by number::

    from telix.scripts import ScriptContext

    async def goto(ctx: ScriptContext, *args: str) -> None:
        if not args:
            ctx.print("[rooms] Usage: `script rooms.goto <room_id>`")
            return
        path = ctx.find_path(args[0])
        if path is None:
            ctx.print(f"[rooms] No path to {args[0]}")
            return
        await ctx.send(";".join(path))

Wait for a pattern then react::

    from telix.scripts import ScriptContext

    async def hunt(ctx: ScriptContext, *args: str) -> None:
        target = args[0] if args else "goblin"
        await ctx.send(f"consider {target}")
        m = await ctx.wait_for(r"seems? .* formidable|is? no match", timeout=5.0)
        if not m or "no match" not in m.group(0):
            ctx.print(f"[hunt] {target} too tough, skipping")
            return
        await ctx.send(f"kill {target}")
        ctx.print("[hunt] fighting!")
