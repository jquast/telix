=========
Scripting
=========

Telix scripts are Python files that run asynchronously using the asyncio
interface.  They are useful for logic that goes beyond the "find pattern and
respond" capability of :doc:`triggers`: hunting loops, healing routines, mapping
runs, or any sequence that depends on timing or server output.

Quickstart
----------

Create a file named ``demo.py`` in the current directory where ``telix`` is
launched or in ``~/.config/telix/scripts/``::

    from telix.scripts import ScriptContext

    async def run(ctx: ScriptContext) -> None:
        room = ctx.room
        ctx.print(f"[demo] You are in: {room.name} ({room.area})")
        ctx.print(f"[demo] Exits: {', '.join(room.exits)}")

Then in the Telix REPL, run it::

    `async demo`

Example output::

    [demo] You are in: Mayor's office (caladan)
    [demo] Exits: west

Starting
--------

Telix looks for scripts in this order:

1. Current working directory
2. ``~/.config/telix/scripts/``

Scripts are started by the `` `async` `` or `` `await` `` commands.  Every time a script is
launched, the last-modified time is checked and reloaded if necessary. Save a script file at any
time and then call it to see any new changes (or errors!).

`` `await NAME` ``
    Start a script and block until it finishes before continuing, preferred for most trigger, macro,
    or autowalk/autodiscover integration this is preferred, as you would want the script to run to
    completion before continuing::

        `await combat.hunt`

`` `async NAME` ``
    Start a script in the background and return immediately.  The script runs
    concurrently with everything else::

        `async combat.hunt`

    This loads ``combat.py`` and calls ``hunt(ctx)`` asynchronously.

With either command calls, the function name can be omitted, and is then assumed as ``run``. 

- `` `async demo` `` loads ``demo.py``, calls ``run(ctx)``.

Scripts can accept optional arguments as strings, using shell-like syntax::

    `async combat.hunt goblin 12345`

And receive them as optional arguments::

    async def hunt(ctx: ScriptContext, target: Optional[str] = None, room_id: Optional[str] = None) -> None:
        ...

These can be used anywhere :doc:`commands` are accepted:

- **REPL**: type `` `async module.func_name` `` at the prompt
- **Trigger reply field**: set reply as `` `async module.func_name` ``
- **Macro text**: include `` `async module.func_name` `` in macro text
- **autowalk** and **autodiscover**: include `` `async module.func_name` `` in room-change command.

Stopping
--------

Asynchronous programming is very useful but can also get out of control, and may accidentally flood
the server with bad commands.

Scripts may be stopped at any time, by another script or by embedded commands,

`` `stopscript` ``
    Stop all running scripts.  Each name is printed as it stops:
    ``[stopscript] stopped: combat.hunt``.

    This command is bound to **Alt + Q** by default.

`` `stopscript combat.hunt` ``
    Stop only the named script.

To stop everything immediately, press **Alt + Q**, a default macro for `` `stopscript` ``

Listing
-------

`` `scripts` ``
    Show the names of all currently running scripts::

        [scripts] running: combat.hunt
        [scripts] running: healer.top_up

    If nothing is running: ``[scripts] no scripts running``.

Chaining
--------

You can launch another script in the background by sending a command through ``ctx.send``::

    await ctx.send("`async hunt`")

Or run another script to completion::

    await ctx.send("`await hunt`")

As python scripts, you may also just import and call functions directly::

    import combat
    await combat.hunt(ctx, "goblin")

See :doc:`scripting-api` for the full ``ctx`` API reference, including
``Room`` and ``RoomStore`` class documentation.

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
        ctx.print(f"[demo] Exits: {', '.join(room.exits)}")

Travel to a room by number::

    from telix.scripts import ScriptContext

    async def goto(ctx: ScriptContext, *args: str) -> None:
        if not args:
            ctx.print("[rooms] Usage: `async rooms.goto <room_id>`")
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

