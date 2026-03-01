Files and directories
=====================

All persistent state follows the `XDG Base Directory Specification
<https://specifications.freedesktop.org/basedir-spec/latest/>`_.  Override
locations with ``$XDG_CONFIG_HOME`` and ``$XDG_DATA_HOME``.

Common defaults:

.. list-table::
   :header-rows: 1

   * - Variable
     - Linux
     - macOS
     - Windows
   * - ``$XDG_CONFIG_HOME``
     - ``~/.config``
     - ``~/Library/Application Support``
     - ``%APPDATA%``
   * - ``$XDG_DATA_HOME``
     - ``~/.local/share``
     - ``~/Library/Application Support``
     - ``%LOCALAPPDATA%``

``$XDG_CONFIG_HOME/telix/`` contains per-feature configuration:

- ``sessions.json``
- ``autoreplies.json``
- ``macros.json``
- ``highlights.json``
- ``progressbars.json`` -- progress bar toolbar configuration

``$XDG_DATA_HOME/telix/`` contains per-session data using a SHA-256 slug
of ``host:port``:

- ``history-<hash>``
- ``rooms-<hash>.db``
- ``chat-<hash>.json``
- ``gmcp-<hash>.json`` -- rolling GMCP data snapshot with per-package timestamps
