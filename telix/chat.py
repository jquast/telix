"""
Chat message persistence for GMCP ``Comm.Channel.Text``.

Provides functions to load, persist, and append chat messages to a per-session JSON file on disk.
"""

# std imports
import os
import json
import typing
import datetime
from typing import TYPE_CHECKING

# local
from . import paths

if TYPE_CHECKING:
    from .session_context import TelixSessionContext

#: Maximum number of chat messages persisted to disk.
CHAT_FILE_CAP = 1000


def load_chat(path: str) -> list[dict[str, typing.Any]]:
    """
    Load chat messages from a JSON file.

    :param path: Path to the chat JSON file.
    :returns: List of message dicts, capped to :data:`CHAT_FILE_CAP`.
    """
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, list):
        return data[-CHAT_FILE_CAP:]
    return []


def persist_chat(path: str, msg: dict[str, typing.Any]) -> None:
    """
    Append a single chat message to the JSON file on disk, capping at :data:`CHAT_FILE_CAP`.

    :param path: Path to the chat JSON file.
    :param msg: Message dict to append.
    """
    msgs = load_chat(path)
    msgs.append(msg)
    if len(msgs) > CHAT_FILE_CAP:
        msgs = msgs[-CHAT_FILE_CAP:]
    paths.atomic_write(path, json.dumps(msgs, ensure_ascii=False) + "\n")


def append_chat_msg(ctx: "TelixSessionContext", data: dict[str, typing.Any]) -> None:
    """
    Append a GMCP ``Comm.Channel.Text`` message to chat state and disk.

    :param ctx: Session context with ``chat_messages``, ``chat_unread``,
        and ``chat_file`` attrs.
    :param data: GMCP message dict with ``channel``, ``talker``, ``text``,
        etc.
    """
    msg: dict[str, typing.Any] = {
        "ts": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "channel": data.get("channel", ""),
        "channel_ansi": data.get("channel_ansi", ""),
        "talker": data.get("talker", ""),
        "text": data.get("text", ""),
    }
    ctx.chat_messages.append(msg)
    ctx.chat_unread += 1
    if len(ctx.chat_messages) > 500:
        ctx.chat_messages[:] = ctx.chat_messages[-500:]
    if ctx.chat_file:
        persist_chat(ctx.chat_file, msg)
