|pypi_downloads| |codecov| |license| |linux| |windows| |mac|

Introduction
============

A modern telnet and WebSocket client designed especially for BBSs_ and MUDs_.


Built using Python libraries telnetlib3_, blessed_, textual_, and wcwidth_.

.. _BBSs: https://bbs.modem.xyz/introduction.html#what-is-a-bbs
.. _MUDs: https://muds.modem.xyz/introduction.html#what-is-a-mud
.. _telnetlib3: https://github.com/jquast/telnetlib3
.. _blessed: https://github.com/jquast/blessed
.. _wcwidth: https://github.com/jquast/wcwidth
.. _textual: https://github.com/Textualize/textual

Features
--------

- **Session manager** with a bundled directory of 1000+ MUD and BBS servers
- **Advanced MUD Features** like macros, autoreplies, highlights, room mapping, travel,
  random walk, autodiscover, progress bars, and chat
- **Advanced Telnet** with SSL/TLS, NAWS, GMCP, MCCP, BINARY, SGA, ECHO, EOR, GA and more
- **WebSocket** connections using the ``gmcp.mudstandards.org`` subprotocol
- **BBS/Scene Art** support for CP437, PETSCII, ATASCII, iCE colors, 24-bit color

Installation
------------

Requires Python 3.9+.

::

    pip install telix

Usage
-----

Launch the Session Manager::

    telix

Connect directly to a host via Telnet::

    telix mud.example.com 4000

Connect directly via WebSocket::

    telix ws://mud.example.com:9119
    telix wss://mud.example.com/ws

Run ``telix --help`` for the full list of options.

Documentation
-------------

Full documentation at https://telix.readthedocs.org/.

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
