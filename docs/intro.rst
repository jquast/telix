|pypi_downloads| |codecov| |license| |linux| |mac| |windows|

Introduction
============

A modern Telnet, WebSocket, and SSH client designed especially for BBSs_ and MUDs_


.. figure:: https://dxtz6bzwq9sxx.cloudfront.net/telix-demo.gif
   :alt: Video recording showing off TUI controls of Telix

Features
--------

- **Session Manager** For configuring and bookmarking connections, bundled with an up-to-date
  directory of over 1,700 MUDs_ and BBSs_.
- **Advanced Telnet** Support for popular BBS and MUD `Protocols
  <https://telix.readthedocs.io/en/latest/protocols.html>`_
- **WebSocket** support for BBS and MUD `websocket subprotocols`_, `TELNETS`_ (Telnet + SSL),
  and SSH protocols are also supported.
- **MUD Features** Easy-to-use TUI interface to create macros, triggers, highlights, room mapping,
  fast travel, random walk, autodiscover, progress bars, chat, and captures through a common
  `Command <https://telix.readthedocs.io/en/latest/commands.html>`_ interface, or advanced
  programming with asyncio Python `Scripting
  <https://telix.readthedocs.io/en/latest/scripting.html>`_.
- **BBS/Scene Art** support for `CP437`_, `PETSCII`_, `ATASCII`_, `iCE colors`_, by translation of
  ANSI color codes and legacy encodings to modern 24-bit color codes and terminal encoding (usually
  utf-8).

Built using Python libraries telnetlib3_, blessed_, textual_, and wcwidth_.

.. _telnetlib3: https://github.com/jquast/telnetlib3
.. _blessed: https://github.com/jquast/blessed
.. _wcwidth: https://github.com/jquast/wcwidth
.. _textual: https://github.com/Textualize/textual
.. _BBSs: https://bbs.modem.xyz/introduction.html#what-is-a-bbs
.. _MUDs: https://muds.modem.xyz/introduction.html#what-is-a-mud
.. _GMCP: https://tintin.mudhalla.net/protocols/gmcp/
.. _MCCP: https://tintin.mudhalla.net/protocols/mccp/
.. _CP437: https://en.wikipedia.org/wiki/Code_page_437
.. _PETSCII: https://en.wikipedia.org/wiki/PETSCII
.. _ATASCII: https://en.wikipedia.org/wiki/ATASCII
.. _iCE colors: https://forum.16colo.rs/t/ice-colors-or-blinking-text/27
.. _`websocket subprotocols`: https://mudstandards.org/websocket/
.. _TELNETS: https://www.micropolis.com/support/kb/micropolis-bbs-faq#Is_there_a_secure_Telnet

Installation
------------

Requires Python 3.10+.

::

    pip install telix

Usage
-----

Launch the Session Manager (TUI)::

    telix

Connect directly to a host via Telnet::

    # Using CLI,
    telix dunemud.net 6789

    # with ssl,
    telix --ssl dunemud.net 6788

Connect via SSH::

    telix ssh://bbs.example.com

    telix ssh://user@bbs.example.com:2222

Connect directly via WebSocket::

    telix wss://xibalba.vip:44512

    telix wss://dev.cryosphere.org:4443/telnet/

.. begin-cli-help
.. code-block:: text

    usage: telix [-h] [--always-do OPT] [--always-dont OPT] [--always-will OPT]
                 [--always-wont OPT] [--ansi-keys] [--ascii-eol] [--compression]
                 [--connect-maxwait N] [--connect-minwait N] [--connect-timeout N]
                 [--encoding ENCODING] [--encoding-errors ENCODING_ERRORS]
                 [--gmcp-modules MODULES] [--line-mode] [--logfile FILE]
                 [--logfile-mode {append,rewrite}] [--loglevel LOGLEVEL]
                 [--no-repl] [--raw-mode] [--send-environ VARS] [--shell SHELL]
                 [--speed N] [--ssl] [--ssl-cafile PATH] [--ssl-no-verify]
                 [--term TERM] [--typescript FILE]
                 [--typescript-mode {append,rewrite}] [--key-file FILE]
                 [--username USER] [--background-color COLOR] [--bbs]
                 [--color-brightness N] [--color-contrast N]
                 [--colormatch PALETTE] [--mud] [--no-ice-colors]

    Telnet, WebSocket, and SSH MUD/BBS client.

      telix host [port]                 -- Telnet
      telix telnet://host[:port]        -- Telnet
      telix telnets://host[:port]       -- Telnet with SSL
      telix ws://host[:port][/path]     -- WebSocket
      telix wss://host[:port][/path]    -- WebSocket with SSL
      telix ssh://[user@]host[:port]    -- SSH

    options:
      -h, --help            show this help message and exit

    Connection options:
      --always-do OPT       always send DO for this option (comma-separated, named
                            like GMCP)
      --always-dont OPT     always send DONT for this option, refusing even
                            natively supported
      --always-will OPT     always send WILL for this option (comma-separated,
                            named like MXP)
      --always-wont OPT     always send WONT for this option, refusing even
                            natively supported
      --ansi-keys           transmit raw ANSI escape sequences for arrow/function
                            keys
      --ascii-eol           use ASCII CR/LF instead of encoding-native EOL
      --compression         request MCCP compression
      --connect-maxwait N   timeout for pending negotiation (default: 4.0)
      --connect-minwait N   shell delay for negotiation (default: 0)
      --connect-timeout N   timeout for connection in seconds (default: 10)
      --encoding ENCODING   encoding name (default: utf-8)
      --encoding-errors ENCODING_ERRORS
                            handler for encoding errors (default: replace)
      --gmcp-modules MODULES
                            comma-separated list of GMCP modules to request
      --line-mode           force line-mode input (default: auto-detect)
      --logfile FILE        write log to FILE
      --logfile-mode {append,rewrite}
                            log file write mode (default: append)
      --loglevel LOGLEVEL   logging level (default: warn)
      --no-repl             disable the interactive REPL (raw I/O only)
      --raw-mode            force raw-mode input (default: auto-detect)
      --send-environ VARS   comma-separated environment variables to send via NEW-
                            ENVIRON
      --shell SHELL         dotted path to shell coroutine
      --speed N             terminal speed to report (default: 38400)
      --ssl                 enable SSL/TLS (telnet only)
      --ssl-cafile PATH     CA bundle for SSL verification (telnet only)
      --ssl-no-verify       disable SSL certificate verification (telnet only)
      --term TERM           terminal type to negotiate (default: $TERM)
      --typescript FILE     record session to FILE
      --typescript-mode {append,rewrite}
                            typescript write mode (default: append)

    SSH options (ssh://[user@]host[:port] connections):
      --key-file FILE       path to private key file
      --username USER       login username (default: system login)

    Telix options:
      --background-color COLOR
                            terminal background color as #RRGGBB (default:
                            #000000)
      --bbs                 apply BBS connection presets
      --color-brightness N  color brightness multiplier (default: 1.0)
      --color-contrast N    color contrast multiplier (default: 1.0)
      --colormatch PALETTE  color palette for remapping (default: vga, 'none' to
                            disable)
      --mud                 apply MUD connection presets
      --no-ice-colors       disable iCE color (blink as bright background) support

.. end-cli-help


Documentation
-------------

Full documentation at https://telix.readthedocs.io/.

.. |pypi_downloads| image:: https://img.shields.io/pypi/dm/telix.svg?logo=pypi
    :alt: Downloads
    :target: https://pypi.org/project/telix/
.. |codecov| image:: https://codecov.io/gh/jquast/telix/branch/master/graph/badge.svg
    :alt: codecov.io Code Coverage
    :target: https://codecov.io/gh/jquast/telix/
.. |license| image:: https://img.shields.io/pypi/l/telix.svg
    :target: https://pypi.org/project/telix/
    :alt: License
.. |linux| image:: https://img.shields.io/badge/Linux-yes-success?logo=linux
    :alt: Linux supported
.. |windows| image:: https://img.shields.io/badge/Windows-yes-success?logo=windows
    :alt: Windows supported
.. |mac| image:: https://img.shields.io/badge/MacOS-yes-success?logo=apple
    :alt: MacOS supported
