## Session Manager

The session manager displays a searchable list of saved telnet and MUD
sessions.  Sessions are sorted by bookmark status, then most recently
connected, then name.

### Buttons

| Button | Action |
|--------|--------|
| **Connect** | Connect to the selected session |
| **New** | Create a new session pre-filled with defaults |
| **Bookmark** | Toggle bookmark on the selected session |
| **Delete** | Delete the selected session |
| **Edit** | Edit the selected session |

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| **Enter** | Connect to the selected session |
| **N** | Create a new session |
| **E** | Edit the selected session |
| **B** | Toggle bookmark on the selected session |
| **D** | Delete the selected session |
| **F1** | Open this help screen |
| **Q** | Quit Telix |

### Search

Type in the search field at the top to filter sessions by name, host,
port, or encoding.  The search matches case-insensitively.  Use the
arrow keys to move between the search field and the session table.

### Bookmarks

Bookmarked sessions are marked with **‡** and sorted to the top of the
list.  Use the Bookmark button or press **B** to toggle the bookmark on
the selected session.

### Flags

The Flags column shows short codes summarizing non-default session
options:

| Flag | Meaning |
|------|---------|
| **ssl** | TLS/SSL connection |
| **raw** | Raw socket mode (no telnet negotiation) |
| **line** | Line mode |
| **!bin** | Binary transfer disabled |
| **ansi** | ANSI key mode |
| **eol** | ASCII line endings |
| **!ice** | iCE colors disabled |
| **!repl** | REPL disabled (display only) |
| **ts** | Typescript recording enabled |

### Session Editing

Press **E** or click Edit to open the session editor.  The editor
allows you to change all session options including host, port, encoding,
connection mode, autoreplies, macros, and more.  Press **N** or click
New to create a new session with default settings.
