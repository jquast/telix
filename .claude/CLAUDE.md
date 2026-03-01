# telix

TUI telnet and MUD client built on top of telnetlib3.

- `docs/intro.rst`: overview, installation, usage
- `docs/session-manager.rst`: TUI session manager
- `docs/contributing.rst`: architecture, module map, integration boundary, development
- `docs/files.rst`: config file paths, XDG layout
- `telix/help/`: user-facing help (commands, macros, autoreplies, highlights, rooms)
- `.editorconfig`: defines basic formatting

## Key rules

- use tox to run tests, linters, and formatters, to ensure requirements are met exactly.
- **telnetlib3 must never import from telix.** Use `writer.ctx` session
  context or callback hooks.
- Max line length: 100 characters
- Sphinx-style reStructuredText docstrings
- Average test coverage expected (~50%) 
  - layout, design, and TUI interaction is not tested
- Write tests first when fixing bugs
- Do not write "defensive exception guards" or "pokemon exception handlers", allow exceptions to
  raise naturally or to catch and handle them exactly.
- After a first draft and successful testing, re-review if the code can be made simpler, to reduce
  size and complexity
- After finishing writing tests, re-review if line length and complexity of tests can be reduced,
  only enough to provide the same amount of coverage, joining related tests, and using parametrized
  testing where possible to reduce length.
