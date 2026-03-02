Contributing
============

We welcome contributions via GitHub pull requests:

- `Fork a Repo <https://help.github.com/articles/fork-a-repo/>`_
- `Creating a pull request
  <https://help.github.com/articles/creating-a-pull-request/>`_

Dependencies
------------

Telix is a TUI telnet and MUD client layered on top of telnetlib3_, blessed_, textual_, and
wcwidth_.  Telix, telnetlib3_, blessed_, and textual_'s dependency rich_ all depend on wcwidth_,
because it's just so darn useful for measuring the width of strings in a terminal.

::

    telix --+--> telnetlib3 ---------+
      |     |                        | 
      +     +--> blessed ------------+----> wcwidth
      |     |                        |
      |     +--> textual --> rich ---+
      |                              |
      +------------------------------+


Jeff Quast is the author of Telix, telnetlib3_, blessed_, and wcwidth_.

Version API
-----------

This project uses `Semantic Versioning <https://semver.org/>`_ for scripting commands and the data
files. This means that all commands and configurations are expected to be backwards-compatible,
though if necessary to do so, the Major version is incremented and released.

This project *does not* follow semantic visioning of any python functions, classes, modules, and **any
of their signatures or names can be changed at any time**. It is not suggested to ``import telix``
for use in any projects, version contracted is for only the CLI and TUI interfaces.

Architecture
------------

**telnetlib3 must never import from Telix.** Use `writer.ctx` session context or callback hooks.

Module map::

    telix/
    ├── main.py                 CLI entry (TUI or direct connect)
    ├── session_context.py      Per-connection mutable state
    ├── client_shell.py         Shell callback (drop-in for telnetlib3)
    │
    ├── client_repl.py          blessed LineEditor REPL event loop
    ├── client_repl_render.py   Toolbar / status line rendering
    ├── client_repl_commands.py Command expansion and backtick dispatch
    ├── client_repl_dialogs.py  Interactive dialogs (confirm, input)
    ├── client_repl_travel.py   Room graph navigation
    ├── repl_theme.py           Textual theme to REPL palette resolution
    │
    ├── client_tui.py           Re-export hub (backwards compat)
    ├── client_tui_base.py      TUI foundation: sessions, base editors, app
    ├── client_tui_editors.py   Macro/autoreply/highlight/bar editors
    ├── client_tui_dialogs.py   Rooms, caps, tabbed editor, dialogs
    │
    ├── autoreply.py            Pattern-triggered automatic responses
    ├── macros.py               Key-bound macro definitions
    ├── highlighter.py          Regex-based output highlighting + captures
    ├── rooms.py                GMCP Room.Info graph store (SQLite)
    ├── chat.py                 GMCP Comm.Channel.Text persistence
    ├── directory.py            Bundled MUD/BBS directory loader
    ├── progressbars.py         Progress bar config loading/saving
    ├── gmcp_snapshot.py        GMCP snapshot persistence
    │
    ├── paths.py                XDG base directory resolution
    ├── util.py                 Small internal helpers
    └── help/                   Markdown help files loaded at runtime

REPL output pipeline
--------------------

The REPL reads server data in ``read_server`` (``client_repl.py``)
using ``await telnet_reader.read()``.  Incoming text flows through
several stages before reaching the terminal:

1. **Telnet parsing** -- ``telnetlib3`` strips IAC sequences and
   decodes bytes to text.  IAC-only segments produce no data; the
   reader stays blocked.

2. **Output transform** -- ``transform_output()`` normalises
   line endings and applies the color filter.

3. **Line hold** -- ``LineHoldBuffer.add(text)`` splits the text
   at the last ``\n``.  Complete lines go to ``emit_now``; the
   trailing fragment (e.g. a prompt without ``\n``) is held back.
   ``schedule_line_hold_flush()`` starts a 150 ms debounce timer
   (``LINE_HOLD_TIMEOUT``).

4. **Prompt signal** -- If the server sends IAC GA or IAC EOR, the
   ``on_prompt_signal`` callback sets ``prompt_pending = True``.
   The main loop flushes held text immediately when it sees a
   pending prompt (``flush_for_prompt``).

5. **Highlight engine** -- ``emit_now`` lines are run through the
   highlight engine before display; held-back text flushed by the
   timer is written raw (no highlights).  Rules with ``captured=True``
   extract regex groups into ``ctx.captures`` (for ``when`` conditions)
   and log matched lines to ``ctx.capture_log`` (for the Capture Window).

6. **Screen output** -- The REPL saves/restores the cursor position
   via VT100 DECSC (``\x1b7``) / DECRC (``\x1b8``), writes to
   ``stdout`` (an ``asyncio.StreamWriter`` connected to the PTY
   master FD via ``connect_write_pipe``), and re-renders the input
   line and toolbar after each write.

7. **Scroll region** -- ``ScrollRegion`` confines server output to
   the top portion of the terminal using DECSTBM
   (``change_scroll_region``).  The input line and toolbar sit
   below the scroll boundary.

   ``grow_reserve()`` expands the reserved area when the GMCP
   toolbar first appears.  It scrolls existing content up by
   emitting newlines at the scroll-region bottom, then adjusts
   the saved cursor position by the same amount so that subsequent
   restore/save pairs stay consistent.

Connection lifecycle
~~~~~~~~~~~~~~~~~~~~

The shell callback (``client_shell.py``) drives the outer
REPL/raw-mode loop:

1. ``telix_client_shell`` is called by telnetlib3 after connection.
2. ``want_repl()`` decides the mode (line vs. kludge/raw).
3. ``repl_event_loop`` sets up the scroll region, registers IAC
   callbacks, and starts ``read_server`` + ``read_input`` as
   concurrent tasks via ``run_repl_tasks``.
4. When the server switches to kludge mode or the connection
   closes, the REPL returns and the outer loop re-evaluates.

Data arriving **before** the REPL event loop starts is buffered in
the telnet reader's internal buffer and consumed by the first
``read()`` call in ``read_server``.

Integration boundary
--------------------

Telix connects to a remote host by calling ``telnetlib3.open_connection()``
with ``--shell=telix.client_shell.telix_client_shell``, a drop-in
replacement for ``telnetlib3.client_shell.telnet_client_shell``.

Every ``TelnetWriter`` has a ``.ctx`` attribute that defaults to a
``TelnetSessionContext``.  Telix's ``SessionContext`` subclasses
``TelnetSessionContext``, adding MUD-specific state (rooms, macros,
highlights, chat, etc.).  The shell callback creates a ``SessionContext``
and assigns it to ``writer.ctx``.

Telix's ``SessionContext`` also provides ``captures`` (a flat
``dict[str, int]`` of captured variables) and ``capture_log`` (a
``dict[str, list[dict]]`` of per-channel capture history), populated by
the highlight engine and consumed by the ``when`` condition checker and
the Capture Window (F10).

``TelnetSessionContext`` (defined in ``telnetlib3/session_context.py``)
provides the attributes that ``telnetlib3.client_shell`` uses:

- ``color_filter`` -- object with ``.filter(str) -> str``
- ``raw_mode`` -- ``None`` (auto-detect), ``True``, or ``False``
- ``ascii_eol`` -- ``bool``
- ``input_filter`` -- ``InputFilter`` or ``None``
- ``autoreply_engine`` -- autoreply engine or ``None``
- ``autoreply_wait_fn`` -- async callable or ``None``
- ``typescript_file`` -- open file handle or ``None``
- ``gmcp_data`` -- ``dict[str, Any]`` of raw GMCP package data

GMCP data flow
~~~~~~~~~~~~~~

GMCP (Generic MUD Communication Protocol) data arrives as telnet
sub-negotiation and is parsed by telnetlib3_ into package/data pairs.
``TelnetClient.on_gmcp()`` stores each package in ``ctx.gmcp_data``
(merging dict updates for the same package key).

Telix overrides the GMCP ext callback in ``telix_client_shell`` to
wrap the base ``on_gmcp`` with package-specific dispatch to callbacks
on ``SessionContext``:

- ``on_chat_text`` -- called for ``Comm.Channel.Text``
- ``on_chat_channels`` -- called for ``Comm.Channel.List``
- ``on_room_info`` -- called for ``Room.Info``

These callback attributes are defined on Telix's ``SessionContext``
and wired up in ``client_shell._load_configs()``.  Access them as
regular attributes -- do not use ``getattr()``.

Room tracking
~~~~~~~~~~~~~

Room state lives in two parallel systems:

1. **In-memory** (for REPL commands like randomwalk, autodiscover,
   and fast-travel): ``ctx.current_room_num``, ``ctx.previous_room_num``,
   ``ctx.room_changed``, and ``ctx.room_graph`` (a ``RoomStore`` backed
   by a SQLite database at ``ctx.rooms_file``).

2. **File-based** (for TUI subprocesses like the F7 room browser):
   ``ctx.current_room_file`` contains the current room number as plain
   text, read by ``rooms.read_current_room()``.  The rooms SQLite DB
   is shared between both systems.

The ``on_room_info`` callback bridges these: when a ``Room.Info`` GMCP
message arrives, it updates ``ctx.current_room_num``, calls
``room_graph.update_room()`` to persist the room and its exits to SQLite,
and writes ``ctx.current_room_file`` so TUI subprocesses see the change.

TUI editor subprocesses
~~~~~~~~~~~~~~~~~~~~~~~

Pressing F-keys (F5-F11) launches Textual-based editor screens in a
**child subprocess** via ``launch_tui_editor()`` in
``client_repl_dialogs.py``.  Key constraints:

- **Never pipe stderr** (``stderr=subprocess.PIPE``).  Textual renders
  its TUI to stderr.  Piping it redirects Textual's output to a pipe
  instead of the terminal, freezing the app because stderr is no
  longer a TTY.

- **Error display**.  Textual stores unhandled exceptions in
  ``app._exception`` and queues Rich tracebacks in
  ``app._exit_renderables``.  In non-pilot mode Textual never calls
  ``print_error_renderables()`` itself, so ``EditorApp`` overrides
  it to write to stdout (not stderr) after the alt screen exits.
  ``run_editor_app()`` calls it explicitly on non-zero return codes.

- **Blocking fds**.  The parent's asyncio event loop sets stdin
  non-blocking.  Since stdin/stdout/stderr share the same PTY file
  description, the child inherits non-blocking mode.
  ``restore_blocking_fds()`` must run before Textual starts.

- **In-band resize (DEC mode 2048)**.  The REPL enables DEC private
  mode 2048 so the terminal sends resize notifications as escape
  sequences instead of (or in addition to) SIGWINCH.  Textual also
  supports this mode and disables it on ``stop_application_mode()``.
  ``restore_after_subprocess()`` must NOT re-enable mode 2048
  immediately -- the terminal responds with a resize notification
  that arrives before the REPL event loop is ready, causing a storm
  of redundant full-screen repaints.  Instead, the module-level flag
  ``subprocess_needs_rearm`` is set, and the main event loop calls
  ``rearm_after_subprocess()`` after the post-action render is
  complete.  That method flushes stale terminal input
  (``termios.tcflush``), records the current terminal size (to
  suppress ``on_resize_repaint``), and only then re-enables mode
  2048.

- **Traceback display**.  ``run_editor_app()`` wraps the Textual
  ``app.run()`` call.  On crash it writes ``TERMINAL_CLEANUP`` (which
  includes cursor-home and clear-screen) and calls ``restore_opost()``
  to re-enable the terminal's ``OPOST`` flag so ``\n`` maps to
  ``\r\n`` -- without this, tracebacks render with staircase output
  because the terminal is still in raw mode.

Developing
----------

Development requires Python 3.10+.  Install in editable mode::

    pip install -e .

Any changes made in this project folder are then made available to the
python interpreter as the ``telix`` module regardless of the current
working directory.

Running Tests
-------------

`Py.test <https://pytest.org>`_ is the test runner.  Install and run using tox::

    pip install --upgrade tox
    tox

Run a single test file::

    tox -e py314 -- telix/tests/test_chat.py -x -v

Code Formatting
---------------

This project uses `ruff <https://docs.astral.sh/ruff/>`_ for code
formatting and linting.  Run it against any new code::

    tox -e format

You can also set up a `pre-commit <https://pre-commit.com/>`_ hook::

    pip install pre-commit
    pre-commit install --install-hooks

Style and Static Analysis
-------------------------

- Do not use single-underscore prefixes on names (functions, classes,
  constants, methods, or attributes). This project has no public Python
  API -- all names are internal. Exceptions:

  - Unused variables in unpacking (e.g. ``for _s, _e, name in spans:``)
  - Property backing attributes (e.g. ``self._enabled`` behind
    ``@property enabled``)
  - External library private attributes (e.g. ``widget._label``,
    ``parser._actions``) which must keep their underscore
  - Dunder methods (``__init__``, ``__enter__``, etc.)

- Import style: ``import module`` everywhere, access via ``module.name``.
  Internal imports use ``from . import module``.  Never ``from X import Y``
  except ``from typing import TYPE_CHECKING`` and inside ``if TYPE_CHECKING:`` blocks.
- do not wrote unicode em-dash, arrows, or similar characters in code or documentation.
- use tox to run tests, linters, and formatters, to ensure requirements are met exactly.
- Max line length: 100 characters
- Sphinx-style reStructuredText docstrings
- Average test coverage expected (~50%)
  - layout, design, and TUI interaction is not tested
- Write tests first when fixing bugs (TDD).
- Do not use "section dividers" or markers for code
- Never use `assert x == y, "expected x, got y"` - pytest output is sufficient
- Test function docstrings should be brief, factual statements of what is tested, not why or how
- Tests should be self-documenting; avoid comments explaining why tests exist
- Do not write defensive `try/except` blocks that swallow errors. Let exceptions
  propagate to the caller unless there is a specific reason to handle them.
- Never catch broad `Exception` or `OSError` just to log and return `None`.
- Acceptable uses: `except ImportError` for optional dependencies, cleanup in
  `finally` blocks, and boundary code that must not crash (e.g. top-level CLI).
- After finishing writing tests, re-review if line length and complexity of tests can be reduced,
  only enough to provide the same amount of coverage, joining related tests, and using parametrized
  testing where possible to reduce length.
- After a first draft of a medium to large change and testing has been successful, re-review if the
  code can be made simpler, to reduce size and complexity, by reducing code
  duplication, use of walrus operators and context manager patterns or
  functional or object oriented design. Ways to reduce so many branches,
  temporary or local variables, or otherwise high mccabe complexity.

Run all linters::

    tox -e lint

Run individual linters::

    tox -e ruff
    tox -e ruff_format
    tox -e pydocstyle
    tox -e codespell

Run all formatters::

    tox -e format

.. _telnetlib3: https://github.com/jquast/telnetlib3
.. _blessed: https://github.com/jquast/blessed
.. _wcwidth: https://github.com/jquast/wcwidth
.. _textual: https://github.com/Textualize/textual


