Contributing
============

We welcome contributions via GitHub pull requests:

- `Fork a Repo <https://help.github.com/articles/fork-a-repo/>`_
- `Creating a pull request
  <https://help.github.com/articles/creating-a-pull-request/>`_

Architecture
------------

telix is a TUI telnet and MUD client layered on top of
`telnetlib3 <https://github.com/jquast/telnetlib3>`_::

    telix  -->  telnetlib3  -->  wcwidth
      |
      +--> blessed, textual

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
    │
    ├── client_tui.py           Textual app (session manager, room browser)
    │
    ├── autoreply.py            Pattern-triggered automatic responses
    ├── macros.py               Key-bound macro definitions
    ├── highlighter.py          Regex-based output highlighting
    ├── rooms.py                GMCP Room.Info graph store (SQLite)
    ├── chat.py                 GMCP Comm.Channel.Text persistence
    ├── directory.py            Bundled MUD/BBS directory loader
    │
    ├── _paths.py               XDG base directory resolution
    ├── _clipboard.py           Clipboard access (xclip/xsel/pbcopy)
    ├── _util.py                Small internal helpers
    └── help/                   Markdown help files loaded at runtime

REPL output pipeline
--------------------

The REPL reads server data in ``_read_server`` (``client_repl.py``)
using ``await telnet_reader.read()``.  Incoming text flows through
several stages before reaching the terminal:

1. **Telnet parsing** — ``telnetlib3`` strips IAC sequences and
   decodes bytes to text.  IAC-only segments produce no data; the
   reader stays blocked.

2. **Output transform** — ``_transform_output()`` normalises
   line endings and applies the color filter.

3. **Line hold** — ``_LineHoldBuffer.add(text)`` splits the text
   at the last ``\n``.  Complete lines go to ``emit_now``; the
   trailing fragment (e.g. a prompt without ``\n``) is held back.
   ``_schedule_line_hold_flush()`` starts a 150 ms debounce timer
   (``_LINE_HOLD_TIMEOUT``).

4. **Prompt signal** — If the server sends IAC GA or IAC EOR, the
   ``_on_prompt_signal`` callback sets ``prompt_pending = True``.
   The main loop flushes held text immediately when it sees a
   pending prompt (``flush_for_prompt``).

5. **Highlight engine** — ``emit_now`` lines are run through the
   highlight engine before display; held-back text flushed by the
   timer is written raw (no highlights).

6. **Screen output** — The REPL saves/restores the cursor position
   via VT100 DECSC (``\x1b7``) / DECRC (``\x1b8``), writes to
   ``stdout`` (an ``asyncio.StreamWriter`` connected to the PTY
   master FD via ``connect_write_pipe``), and re-renders the input
   line and toolbar after each write.

7. **Scroll region** — ``ScrollRegion`` confines server output to
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
2. ``_want_repl()`` decides the mode (line vs. kludge/raw).
3. ``repl_event_loop`` sets up the scroll region, registers IAC
   callbacks, and starts ``_read_server`` + ``_read_input`` as
   concurrent tasks via ``_run_repl_tasks``.
4. When the server switches to kludge mode or the connection
   closes, the REPL returns and the outer loop re-evaluates.

Data arriving **before** the REPL event loop starts is buffered in
the telnet reader's internal buffer and consumed by the first
``read()`` call in ``_read_server``.

Integration boundary
--------------------

telix connects to a remote host by calling ``telnetlib3.open_connection()``
with ``--shell=telix.client_shell.telix_client_shell``, a drop-in
replacement for ``telnetlib3.client_shell.telnet_client_shell``.

Every ``TelnetWriter`` has a ``.ctx`` attribute that defaults to a
``TelnetSessionContext``.  telix's ``SessionContext`` subclasses
``TelnetSessionContext``, adding MUD-specific state (rooms, macros,
highlights, chat, etc.).  The shell callback creates a ``SessionContext``
and assigns it to ``writer.ctx``.

``TelnetSessionContext`` (defined in ``telnetlib3/_session_context.py``)
provides the attributes that ``telnetlib3.client_shell`` uses:

- ``color_filter`` -- object with ``.filter(str) -> str``
- ``raw_mode`` -- ``None`` (auto-detect), ``True``, or ``False``
- ``ascii_eol`` -- ``bool``
- ``input_filter`` -- ``InputFilter`` or ``None``
- ``autoreply_engine`` -- autoreply engine or ``None``
- ``autoreply_wait_fn`` -- async callable or ``None``
- ``typescript_file`` -- open file handle or ``None``

Developing
----------

Development requires Python 3.9+.  Install in editable mode::

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

This project uses `black <https://github.com/psf/black/>`_ for code
formatting.  Run it against any new code::

    tox -e black

You can also set up a `pre-commit <https://pre-commit.com/>`_ hook::

    pip install pre-commit
    pre-commit install --install-hooks

Style and Static Analysis
-------------------------

- Max line length: 100 characters
- Sphinx-style reStructuredText docstrings
- High test coverage expected (>50%); write tests first when fixing bugs

Run all linters::

    tox -e lint

Run individual linters::

    tox -e flake8
    tox -e isort_check
    tox -e pydocstyle
    tox -e pylint
    tox -e codespell

Run all formatters::

    tox -e format
