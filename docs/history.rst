History
=======

0.1.6 -- 2026-03-15
-------------------

- bugfix vga colormatch for background colors, eg. xibalba bbs main menu.
- bugfix typescript file became 0 bytes when telnet linemode/raw switched.

0.1.5 -- 2026-03-14
-------------------

- enhancement: Add cp1252 encoding (Medievia)

0.1.4 -- 2026-03-13
-------------------

- bugfix: Make more effort to track rooms on servers like Medievia that do not serve any room id's,
  and, remove erroneously assigned "pk"

0.1.3 -- 2026-03-13
-------------------

- enhancement: support non-GMCP complaint room keys, ("vnum", "id", "pk") (Medievia)

0.1.2 -- 2026-03-13
-------------------

- bugfix: progress bar TUI was silently disappearing on edit.
- bugfix: cmd.exe failing to send any TERM type, now sends "ansi"
- bugfix: MTTS bitvector now declares 256-color support when truecolor
- enhancement: selecting type "Mud" now sends TERM=XTERM-TRUECOLOR by default

0.1.1 -- 2026-03-12
--------------------

- bugfix: GMCP package names by title-casing ``char.vitals`` -> ``Char.Vitals``,
  fixes room data and progress bars for Aardwolf (and probably others).

0.1.0 -- 2026-03-09
--------------------

- Initial public alpha release.
