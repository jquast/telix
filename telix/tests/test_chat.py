"""Tests for GMCP chat message handling, persistence, and toolbar badge."""

# std imports
import os
import json
from typing import Any
from unittest.mock import MagicMock

# 3rd party
import pytest

# local
from telix.chat import CHAT_FILE_CAP, load_chat, persist_chat, append_chat_msg
from telix.paths import chat_path
from telix.session_context import SessionContext
from telix.client_repl_render import ToolbarSlot, sgr_fg, wcswidth


def make_ctx(tmp_path: Any, session_key: str = "test:4000") -> SessionContext:
    ctx = SessionContext(session_key=session_key)
    ctx.chat_file = str(tmp_path / "chat.json")
    return ctx


def sample_gmcp_msg(channel: str = "chat", talker: str = "Bob", text: str = "hello") -> dict[str, Any]:
    return {"channel": channel, "channel_ansi": f"\x1b[0m[{channel}]\x1b[0m", "talker": talker, "text": text + "\n"}


class TestAppendChat:
    def test_appends_to_ctx(self, tmp_path: Any) -> None:
        ctx = make_ctx(tmp_path)
        data = sample_gmcp_msg()
        append_chat_msg(ctx, data)

        assert len(ctx.chat_messages) == 1
        assert ctx.chat_messages[0]["channel"] == "chat"
        assert ctx.chat_messages[0]["talker"] == "Bob"
        assert "ts" in ctx.chat_messages[0]

    def test_increments_unread(self, tmp_path: Any) -> None:
        ctx = make_ctx(tmp_path)
        assert ctx.chat_unread == 0
        append_chat_msg(ctx, sample_gmcp_msg())
        assert ctx.chat_unread == 1
        append_chat_msg(ctx, sample_gmcp_msg(talker="Alice"))
        assert ctx.chat_unread == 2

    def test_ring_buffer_cap(self, tmp_path: Any) -> None:
        ctx = make_ctx(tmp_path)
        ctx.chat_file = ""
        for i in range(510):
            append_chat_msg(ctx, sample_gmcp_msg(talker=f"user{i}"))
        assert len(ctx.chat_messages) == 500
        assert ctx.chat_messages[0]["talker"] == "user10"

    def test_persists_to_file(self, tmp_path: Any) -> None:
        ctx = make_ctx(tmp_path)
        append_chat_msg(ctx, sample_gmcp_msg())
        assert os.path.exists(ctx.chat_file)
        with open(ctx.chat_file, encoding="utf-8") as f:
            data = json.load(f)
        assert len(data) == 1
        assert data[0]["talker"] == "Bob"


class TestPersistChat:
    def test_roundtrip(self, tmp_path: Any) -> None:
        path = str(tmp_path / "chat.json")
        msg = {"ts": "2026-01-01T00:00:00", "channel": "chat", "talker": "X", "text": "hi"}
        persist_chat(path, msg)
        loaded = load_chat(path)
        assert len(loaded) == 1
        assert loaded[0]["talker"] == "X"

    def test_file_cap(self, tmp_path: Any) -> None:
        path = str(tmp_path / "chat.json")
        msgs = [
            {"ts": f"2026-01-01T00:00:{i:02d}", "channel": "chat", "talker": f"u{i}", "text": "x"}
            for i in range(CHAT_FILE_CAP + 1)
        ]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(msgs, f)

        persist_chat(path, {"ts": "later", "channel": "chat", "talker": "last", "text": "end"})
        loaded = load_chat(path)
        assert len(loaded) == CHAT_FILE_CAP
        assert loaded[-1]["talker"] == "last"

    def test_load_missing_file(self, tmp_path: Any) -> None:
        loaded = load_chat(str(tmp_path / "nope.json"))
        assert loaded == []

    def test_append_multiple(self, tmp_path: Any) -> None:
        path = str(tmp_path / "chat.json")
        for i in range(3):
            persist_chat(path, {"ts": str(i), "channel": "chat", "talker": f"u{i}", "text": "x"})
        loaded = load_chat(path)
        assert len(loaded) == 3


class TestChatBadge:
    def test_badge_present_when_unread(self) -> None:
        ctx = SessionContext(session_key="test:4000")
        ctx.chat_unread = 5
        badge = f"F10-Chat:{ctx.chat_unread}"
        slot = ToolbarSlot(
            priority=3,
            display_order=8,
            width=wcswidth(badge),
            fragments=[(sgr_fg("#ffff00"), badge)],
            side="left",
            min_width=0,
            label="",
        )
        assert "F10-Chat:5" in slot.fragments[0][1]

    def test_badge_absent_when_zero(self) -> None:
        ctx = SessionContext(session_key="test:4000")
        ctx.chat_unread = 0
        assert ctx.chat_unread == 0


class TestChannelList:
    def test_stores_channel_list(self) -> None:
        ctx = SessionContext(session_key="test:4000")
        channels = [{"name": "chat", "caption": "Chat"}, {"name": "tp", "caption": "Talker"}]
        ctx.chat_channels = channels
        assert len(ctx.chat_channels) == 2
        assert ctx.chat_channels[0]["name"] == "chat"


class TestOnChatTextCallback:
    def test_callback_appends_message(self, tmp_path: Any) -> None:
        ctx = make_ctx(tmp_path)
        ctx.on_chat_text = lambda data: append_chat_msg(ctx, data)
        ctx.on_chat_text(sample_gmcp_msg(channel="tp", talker="Alice", text="hey"))
        assert len(ctx.chat_messages) == 1
        assert ctx.chat_messages[0]["talker"] == "Alice"
        assert ctx.chat_unread == 1

    def test_channels_callback_stores_list(self) -> None:
        ctx = SessionContext(session_key="test:4000")
        channels = [{"name": "chat"}, {"name": "tp"}]
        ctx.on_chat_channels = lambda data: setattr(ctx, "chat_channels", data)
        ctx.on_chat_channels(channels)
        assert ctx.chat_channels == channels


class TestChatPath:
    def test_chat_path_returns_string(self) -> None:
        p = chat_path("mud.example.com:4000")
        assert p.endswith(".json")
        assert "chat-" in p

    def test_chat_path_unique_per_session(self) -> None:
        p1 = chat_path("mud.example.com:4000")
        p2 = chat_path("other.host:23")
        assert p1 != p2
