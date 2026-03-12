Scripting API
=============

The ``ctx`` object
------------------

Every script receives a ``ctx`` argument. This is a "God variable", and provides
access to all known information about the MUD Session and scripting capabilities
of Telix.

Send
~~~~

``await ctx.send(line, wait_prompt=True)``
    Send a command to the server.  Supports the same syntax as the REPL:

    - ``;`` between commands waits for the server's prompt before sending the next
    - ``|`` sends immediately without waiting
    - A leading number repeats: ``3north`` will send ``north`` times

    Backtick directives like `` `async` `` and `` `until` `` are handled by the client, not sent to
    the server.  See :doc:`commands` for the full list of available backtick commands.

    By default, ``send`` waits for the server's prompt (GA/EOR) after all
    commands have been dispatched.  Pass ``wait_prompt=False`` to return
    immediately without waiting::

        await ctx.send("look")              # waits for prompt
        await ctx.send("look", wait_prompt=False)  # returns immediately

Prompts
~~~~~~~

``await ctx.prompt(timeout=None)``
    Wait for the server's next prompt.  Returns ``True`` if it arrived within
    the timeout, ``False`` on timeout.  Pass a number of seconds to set a
    deadline; the default ``None`` waits indefinitely.

``await ctx.prompts(n, timeout=None)``
    Wait for *n* prompts in a row.  Useful for pacing a sequence of commands.

Server Output
~~~~~~~~~~~~~

``ctx.output(clear=True)``
    Return everything the server has sent since the script started, as a single
    string.  The buffer is cleared afterwards by default, so the next call only
    shows new output.  Pass ``clear=False`` to read without clearing.

``ctx.turns(n=5)``
    Return the last *n* chunks of server output, split at each prompt.  Useful
    when you want to inspect the response to a specific command.

Terminal output
~~~~~~~~~~~~~~~

``ctx.print(*args, sep=" ")``
    Print a message to the terminal in cyan.  Works like Python's built-in ``print``: pass multiple
    values and they are joined with *sep* and coerced to string.

``ctx.log(msg)``
    Write a message to the log file at INFO level.


Pattern matching
~~~~~~~~~~~~~~~~

``await ctx.wait_for(pattern, timeout=None)``
    Wait for a line of server output matching *pattern* (a regular expression).
    Returns the match object when found, or ``None`` on timeout.

Conditions
~~~~~~~~~~

``await ctx.condition_met(key, op, threshold, timeout=None)``
    Wait until a numeric condition becomes true.  Returns ``True`` when the
    condition is met, ``False`` on timeout.  *op* is one of ``">"``, ``"<"``,
    ``">="``, ``"<="``, ``"="``.

    *key* resolves in this order:

    1. **Common vitals** -- ``"HP%"``, ``"MP%"``, ``"HP"``, ``"MP"`` are
       computed from ``Char.Vitals`` using a set of known field aliases
       (``hp``/``maxhp``, ``mana``/``maxmp``, etc.).
    2. **Any GMCP percentage** -- append ``%`` to any field name performs a search for a matching
       ``Foo`` / ``MaxFoo`` pair and computes the ratio.  Both fields must live in the same package
       dict::

           await ctx.condition_met("Water%", "<", 50)
           # works if Char.Guild.Stats contains both "Water" and "MaxWater"

    3. **Any GMCP raw value** -- the bare field name is searched across all
       package dicts.
    4. **Highlight capture variable** -- any variable captured by a
       highlight rule, by name (or ``Name`` / ``MaxName`` for ``%``).

    *key* may also be a fully-qualified dotted path, which bypasses the search
    and resolves directly::

        await ctx.condition_met("Char.Guild.Stats.Water%", "<", 50)

``await ctx.conditions_met(*conditions, timeout=None)``
    Wait until multiple conditions are all true **at the same time**.  Each
    condition is a ``(key, op, threshold)`` tuple using the same syntax as
    ``condition_met``.  Returns ``True`` when all conditions hold, ``False``
    on timeout::

        await ctx.conditions_met(
            ("Water%", ">", 0),
            ("HP%", "<", 100),
            timeout=30.0,
        )

    Unlike running separate ``condition_met`` calls with ``asyncio.wait``,
    this checks all conditions atomically, so you are guaranteed they all hold
    simultaneously when it returns.

GMCP data
~~~~~~~~~

``ctx.gmcp``
    The full GMCP data dictionary, as received from the server.

``ctx.gmcp_get(path)``
    Read a value out of the GMCP data by path, e.g.
    ``ctx.gmcp_get("Char.Vitals.hp")``.  Returns ``None`` if not found.

    A bare field name without dots searches across all GMCP packages::

        hp = ctx.gmcp_get("hp")          # finds hp in Char.Vitals
        water = ctx.gmcp_get("Water")    # finds Water in Char.Guild.Stats

    If the path ends with ``%``, the value is computed as a ratio of the field
    to its ``Max`` counterpart, returned as a float between 0.0 and 1.0.
    Both dotted and bare forms work::

        ctx.gmcp_get("Char.Guild.Stats.Water%")  # e.g. 0.95
        ctx.gmcp_get("Water%")                   # same result

    The ``Max`` lookup is case-insensitive (``MaxWater``, ``maxwater``, etc.).

``await ctx.gmcp_changed(package=None, timeout=None)``
    Wait until the next GMCP packet is received.  Pass a package name to wait
    for a specific package, or omit it (or pass ``None``) to wait for any
    GMCP update.  Returns ``True`` if a packet arrived, ``False`` on timeout::

        async def watch_vitals(ctx: ScriptContext) -> None:
            while True:
                if not await ctx.gmcp_changed("Char.Vitals", timeout=60.0):
                    break
                hp = ctx.gmcp_get("Char.Vitals.hp")
                ctx.print(f"[vitals] HP: {hp}")

        # wait for any GMCP update
        await ctx.gmcp_changed()

Room graph
~~~~~~~~~~

``ctx.room_id``
    The current room's number, as a string.

``ctx.previous_room_id``
    The number of the room you were in before the current one.

``ctx.room``
    The current :class:`~telix.rooms.Room` object, or ``None`` if
    GMCP room data has been received.  The room object has:

    - ``room.name`` -- room name string
    - ``room.area`` -- area name string
    - ``room.exits`` -- ``{direction: room_num}`` dict of known exits

``ctx.room_graph``
    The full :class:`~telix.rooms.RoomStore` -- *all* rooms mapped for this session.

``ctx.get_room(num)``
    Look up any room by its number.

``ctx.find_path(dst)``
    Find directions from the current room to *dst*.  Returns a list of
    direction strings, or ``None`` if no route is known.

``await ctx.room_changed(timeout=None)``
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
    Stop any active walk::

        async def scout(ctx: ScriptContext, *args: str) -> None:
            if ctx.walk_active:
                ctx.stop_walk()

Script control
~~~~~~~~~~~~~~

``ctx.running_scripts``
    A list of the names of all currently running scripts, e.g.
    ``["combat.hunt", "healer"]``.  The name is the first token
    of the spec used to start the script.  The calling script's
    own name is included in the list.

Room
----

.. autoclass:: telix.rooms.Room
   :members:

RoomStore
---------

.. autoclass:: telix.rooms.RoomStore
   :members: rooms, get_room, has_room, find_path, search, find_same_name,
             get_home_for_area, blocked_rooms, room_nums,
             toggle_bookmark, toggle_blocked, toggle_home, toggle_marked
