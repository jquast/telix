Protocols
---------

Telix supports Telnet, Websockets, and SSH.

Telnet
~~~~~~

Telix supports the following relevant RFCs:

* :rfc:`854` Telnet Protocol Specification
* :rfc:`855` Telnet Option Specifications
* :rfc:`856` Telnet Binary Transmission (BINARY)
* :rfc:`857` Telnet Echo Option (ECHO)
* :rfc:`858` Telnet Suppress Go Ahead Option (SGA)
* :rfc:`885` Telnet End of Record Option (EOR)
* :rfc:`1073` Telnet Window Size Option (NAWS)
* :rfc:`1079` Telnet Terminal Speed Option (TSPEED)
* :rfc:`1091` Telnet Terminal-Type Option (TTYPE)
* :rfc:`1408` Telnet Environment Option (ENVIRON)
* :rfc:`1572` Telnet Environment Option (NEW_ENVIRON)
* :rfc:`2066` Telnet Charset Option (CHARSET)

Mud
~~~

The following MUD standards are supported:

* `MTTS`_ -- Mud Terminal Type Standard.  Client capability bitvector
  advertised via TTYPE cycling.
* `MNES`_ -- Mud New Environment Standard.  Structured client/server
  variable exchange over NEW-ENVIRON.
* `GMCP`_ -- Generic MUD Communication Protocol.  JSON-based
  bidirectional messaging for game data.
* `MSDP`_ -- MUD Server Data Protocol.  Structured key-value protocol
  for game variables.
* `MSSP`_ -- MUD Server Status Protocol.  Server metadata for MUD
  crawlers and directories.
* `MCCP`_ -- MUD Client Compression Protocol (v2 and v3).  Zlib
  compression for server-to-client and client-to-server data.
* `EOR`_ -- End of Record.  Marks the end of a prompt so the client
  can distinguish prompts from regular output.

.. _MTTS: https://tintin.mudhalla.net/protocols/mtts/
.. _MNES: https://tintin.mudhalla.net/protocols/mnes/
.. _GMCP: https://tintin.mudhalla.net/protocols/gmcp/
.. _MSDP: https://tintin.mudhalla.net/protocols/msdp/
.. _MSSP: https://tintin.mudhalla.net/protocols/mssp/
.. _MCCP: https://tintin.mudhalla.net/protocols/mccp/
.. _EOR: https://tintin.mudhalla.net/protocols/eor/


