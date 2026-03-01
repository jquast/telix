telix
=====

A modern telnet client especially designed for BBSs_ and MUDs_.

Built using python libraries telnetlib3_, blessed_, and textual_.

.. _BBSs: https://bbs.modem.xyz/
.. _MUDs: https://muds.modem.xyz/
.. _telnetlib3: https://github.com/jquast/telnetlib3
.. _blessed: https://github.com/jquast/blessed
.. _textual: https://github.com/Textualize/textual

.. contents:: Table of Contents
   :local:
   :depth: 2

Features
--------

- **Session manager** -- A TUI for browsing, creating, and launching sessions.  Ships with a bundled
  directory of 1000+ MUD and BBS servers.
- **Advanced Telnet** -- Thanks to telnetlib3_, possibly supporting more telnet RFCs and extensions
  than any other client in the world! SSL/TLS, NAWS, NEW_ENVIRON, CHARSET, GMCP, MSSP, BINARY, SGA,
  ECHO, TTYPE, NEW_ENVIRON, TSPEED, CHARSET, LINEMODE, XDISPLOC, EOR, GA and more!
- **BBS Scene Art Support** -- Telix can translate popular BBS encodings like CP437, PETSCII, and
  ATASCII to modern utf-8 terminals. It can also translate color codes, including iCE colors, to
  24-bit color terminal sequences for accurate color accuracy of "Scene art".
- **Strong Mud Support**: dedicated TUI to configure Macros, Autoreplies, Highlights, Room mapping,
  Fast travel, Random walk, Auto-Discover, and Chat.

Installation
------------

Requires Python 3.9+.

::

    pip install telix

Usage
-----

Launch the TUI session manager::

    telix

Connect directly to a host::

    telix mud.example.com 4000

All ``telnetlib3`` client flags are passed through (``--encoding``, ``--ssl``, ``--loglevel``,
etc.).  Run ``telix --help`` for the full list.

Session manager
---------------

When launched without a host argument, telix opens a Textual-based session manager.  The left panel
lists saved sessions; selecting one shows its connection details on the right. Connect using

Each session stores per-host options: encoding (utf-8, cp437, latin-1, and more), SSL/TLS with
optional certificate verification, raw or line mode, connection timeout, environment variables,
telnet option negotiation, color matching, background color, and ICE colors.

The session list is persisted to ``~/.config/telix/sessions.json``.

Keyboard shortcuts
------------------

Session keys (always available):

========== ================================
Key        Action
========== ================================
F1         Help
Ctrl+]     Disconnect
========== ================================

Hotkeys of REPL mode, activated with "Advanced REPL" option enabled (default) and the server is
negotiated in LINEMODE (MUDs):

========== ================================
Key        Action
========== ================================

F6         Edit highlights
Shift+F6   Toggle highlights on/off
F8         Edit macros
F9         Edit autoreplies
Shift+F9   Toggle autoreplies on/off
Ctrl+L     Repaint screen (in REPL/linemode)

Line editing keys of REPL mode:

=========== ================================
Key         Action
=========== ================================
Left/Right  Move cursor
Home/Ctrl+A Beginning of line
End/Ctrl+E  End of line
Ctrl+Left   Word left
Ctrl+Right  Word right
Backspace   Delete before cursor
Delete      Delete at cursor
Ctrl+K      Kill to end of line
Ctrl+U      Kill entire line
Ctrl+W      Kill word back
Ctrl+Y      Yank (paste kill ring)
Ctrl+C      Copy line to clipboard (OSC 52)
Ctrl+V      Paste from clipboard
Ctrl+Z      Undo
Up/Down     History navigation
=========== ================================

GMCP hotkeys available when server supports GMCP negotiation (MUDs):

========== ================================
Key        Action
========== ================================
F3         Random walk
F4         Autodiscover
F5         Resume last walk
F7         Room browser / fast travel
========== ================================

Command syntax
--------------

Commands can be chained with separators and repeated with a numeric prefix.

Separators
~~~~~~~~~~

``;`` (semicolon)
    Send the next command after the server sends a prompt (GA/EOR).  This is
    the standard pacing separator.

``:`` (colon)
    Send the next command immediately without waiting for a prompt.

Repeat prefix
~~~~~~~~~~~~~

A leading integer repeats the next token::

    3n;2e         →  n;n;n;e;e
    5attack       →  attack;attack;attack;attack;attack

Backtick commands
~~~~~~~~~~~~~~~~~

Client-side directives enclosed in backticks, evaluated before sending.

``delay``
    Pause execution: `` `delay 1s` ``, `` `delay 500ms` ``.

``when``
    Conditional gate on GMCP vitals (percentages of max):
    `` `when HP%>=80` ``, `` `when MP%>50` ``.

``until``
    Wait for a regex pattern in server output (case-insensitive, default
    timeout 4 seconds): `` `until died\.` ``, `` `until 10 treasure` ``.

``untils``
    Same as ``until`` but case-sensitive.

``fast travel <room_id>`` / ``slow travel <room_id>``
    Navigate to a room by GMCP ID.  Fast skips exclusive autoreplies; slow
    allows them to fire at each room.

``return fast`` / ``return slow``
    Travel back to the room where the current macro started.

``home``
    Fast travel to the home room of the current area.

``autodiscover [limit]``
    BFS-explore unvisited exits from nearby rooms.

``randomwalk [limit]``
    Walk randomly, preferring rooms with unvisited exits.

``resume [limit]``
    Resume the last autodiscover or randomwalk from where it stopped.

Macros
------

Macros bind a keystroke to a command sequence that executes exactly as if
typed at the input line.  Press **F8** to open the macro editor.

- Command text supports all separators, repeat prefixes, and backtick
  commands.
- Macros can be enabled, disabled, copied, reordered, and sorted by
  last-used time.
- Supported keys: F1--F12, Alt+letter, Ctrl+letter, and single characters.
- Macros are stored per-session in ``~/.config/telix/macros.json``.

Autoreplies
-----------

Autoreplies fire automatic commands when a regex pattern matches server
output.  Press **F9** to open the autoreply editor.

Flags:

- **A (Always)** -- Match even while another rule's exclusive chain is active.
- **I (Immediate)** -- Reply without waiting for a GA/EOR prompt.
- **C (Case-sensitive)** -- Case-sensitive pattern matching.
- **W (When)** -- Attach a vital-percentage condition gate.

Patterns use Python regex syntax.  Capture groups (``\1``, ``\2``) can be
interpolated into the reply text.

Rules are evaluated top-to-bottom; the first match wins unless a rule is
marked Always.  Stored per-session in ``~/.config/telix/autoreplies.json``.

Highlights
----------

Highlights colorize server output when regex patterns match.  Press **F6**
to open the highlight editor.

Styles are composed from attributes and colors separated by underscores:

- Attributes: ``bold``, ``italic``, ``underline``, ``blink``, ``reverse``
- Foreground: ``red``, ``green``, ``yellow``, ``blue``, ``magenta``,
  ``cyan``, ``white``, ``black``
- Background: prefix with ``on_`` (e.g. ``on_yellow``)

Examples: ``bold_red``, ``underline_green``, ``bold_white_on_red``.

Flags:

- **S (Stop)** -- Cancel any active autodiscover or randomwalk on match.
- **C (Case-sensitive)** -- Case-sensitive pattern matching.

Stored per-session in ``~/.config/telix/highlights.json``.

Room mapping
------------

When the server sends GMCP ``Room.Info`` messages, telix builds an
incrementally-growing room graph stored in SQLite at
``~/.local/share/telix/rooms-<host>_<port>.db``.

The room graph supports:

- BFS shortest-path navigation (fast travel, slow travel)
- Autodiscover (BFS-explore unvisited exits)
- Random walk (prefer rooms with unvisited exits)
- Room markers: bookmarks, blocks (excluded from pathfinding), home (one per
  area), and visual marks
- Blocked exits to prevent travel through dangerous areas
- ID rotation detection for rooms that change hash each visit

Press **F7** to open the room browser.  Rooms can be filtered by area,
sorted by name/ID/distance/last-visited, and traveled to directly.

Chat
----

When the server sends GMCP ``Comm.Channel.Text`` messages, telix persists
them to ``~/.config/telix/chat-<host>_<port>.json`` (capped at the 1000 most
recent messages) and tracks unread counts.

Configuration files
-------------------

All persistent state uses XDG base directories:

``~/.config/telix/``
    ``sessions.json``, ``autoreplies.json``, ``macros.json``,
    ``highlights.json``

``~/.local/share/telix/``
    ``history-<hash>`` (command history), ``rooms-<host>_<port>.db``
    (room graph), ``chat-<host>_<port>.json`` (chat log)

Session-specific files use a SHA-256 slug of ``host:port`` for safe
filesystem naming.

Contributing
------------

See ``CONTRIBUTING.rst``.

License
-------

ISC.  See ``LICENSE.txt``.
