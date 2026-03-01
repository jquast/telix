Introduction
============

A modern telnet client designed especially for BBSs_ and MUDs_.

Built using Python libraries telnetlib3_, blessed_, and textual_.

.. _BBSs: https://bbs.modem.xyz/introduction.html#what-is-a-bbs
.. _MUDs: https://muds.modem.xyz/introduction.html#what-is-a-mud
.. _telnetlib3: https://github.com/jquast/telnetlib3
.. _blessed: https://github.com/jquast/blessed
.. _textual: https://github.com/Textualize/textual

Features
--------

- **Session manager** with a bundled directory of 1000+ MUD and BBS servers
- **Advanced Telnet** — SSL/TLS, NAWS, GMCP, MCCP, BINARY, SGA, ECHO, EOR, GA and more
- **BBS/Scene Art** — CP437, PETSCII, ATASCII, iCE colors, 24-bit color
- **MUD Support** — macros, autoreplies, highlights, room mapping, fast/slow travel,
  random walk, autodiscover, and chat

Installation
------------

Requires Python 3.9+.

::

    pip install telix

Usage
-----

Launch the Session Manager::

    telix

Connect directly to a host::

    telix mud.example.com 4000

Run ``telix --help`` for the full list of options.

Documentation
-------------

Full documentation at https://telix.readthedocs.org/.
