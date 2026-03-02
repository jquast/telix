Session manager
===============

When launched without a host argument, telix opens a Textual-based session manager.

This is a traditional "Dialing Directory" of host/port combinations and their settings
which may be set accordingly to preference of the remote system (BBS or MUD):

- Encoding (eg. utf-8, cp437, latin-1, gbk)
- SSL/TLS
- ICE colors (BBS)
- vga color matching (BBS)
- raw (BBS) or line mode (MUDs)
- Advanced REPL (MUDs)

To connect to a system, use the mouse to click the selected entry, or select
using keyboard and press return.

Once connected, disconnect using ``Control  + ]``, which returns to the session manager.
