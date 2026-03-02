"""Help topic loader for telix TUI."""

# std imports
import importlib.resources


def read_topic(name: str) -> str:
    """Read a help topic markdown file by name (without .md extension)."""
    ref = importlib.resources.files(__package__).joinpath(f"{name}.md")
    return ref.read_text(encoding="utf-8")


def get_help(topic: str) -> str:
    """
    Return combined help text for a TUI help topic.

    :param str topic: One of ``"macro"``, ``"autoreply"``, ``"highlight"``,
        ``"room"``, or ``"keybindings"``.
    :rtype: str
    """
    commands = read_topic("commands")
    if topic == "macro":
        return read_topic("macros") + "\n---\n\n" + commands
    if topic == "autoreply":
        return read_topic("autoreplies") + "\n---\n\n" + commands
    if topic == "highlight":
        return read_topic("highlights")
    if topic == "room":
        return read_topic("rooms")
    if topic == "room-mapping":
        return read_topic("room-mapping")
    if topic == "keybindings":
        return read_topic("keybindings")
    if topic == "progressbar":
        return read_topic("progressbars")
    if topic == "session":
        return read_topic("sessions")
    raise ValueError(f"unknown help topic: {topic!r}")
