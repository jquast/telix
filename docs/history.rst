History
=======

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
