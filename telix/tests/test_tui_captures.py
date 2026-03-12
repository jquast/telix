"""Tests for telix.client_tui_captures data logic."""

from __future__ import annotations

import json
import os

import pytest

from telix import client_tui_captures


class TestLoadMessages:
    """CapsPane.load_messages from JSON files."""

    def test_chat_file(self, tmp_path):
        chat_path = str(tmp_path / "chat.json")
        messages = [
            {"channel": "gossip", "talker": "Alice", "text": "hello", "ts": "2025-01-01T00:00:00"},
            {"channel": "ooc", "talker": "Bob", "text": "world", "ts": "2025-01-01T00:01:00"},
            {"channel": "gossip", "talker": "Carol", "text": "hi", "ts": "2025-01-01T00:02:00"},
        ]
        with open(chat_path, "w", encoding="utf-8") as fh:
            json.dump(messages, fh)
        pane = client_tui_captures.CapsPane(chat_file=chat_path)
        pane.load_messages()
        assert len(pane.messages) == 3
        assert sorted(pane.channels) == ["gossip", "ooc"]

    def test_capture_file(self, tmp_path):
        cap_path = str(tmp_path / "captures.json")
        cap_data = {
            "captures": {"kills": 5, "gold": 100},
            "capture_log": {"kills": [{"line": "You slay a goblin", "ts": "2025-01-01T00:00:00"}]},
        }
        with open(cap_path, "w", encoding="utf-8") as fh:
            json.dump(cap_data, fh)
        pane = client_tui_captures.CapsPane(chat_file="", capture_file=cap_path)
        pane.load_messages()
        assert pane.captures == {"kills": 5, "gold": 100}
        assert "kills" in pane.capture_log
        assert "kills" in pane.channels

    def test_missing_files(self, tmp_path):
        pane = client_tui_captures.CapsPane(
            chat_file=str(tmp_path / "no_chat.json"),
            capture_file=str(tmp_path / "no_caps.json"),
        )
        pane.load_messages()
        assert pane.messages == []
        assert pane.captures == {}
        assert pane.capture_log == {}
        assert pane.channels == []

    def test_empty_chat_file(self, tmp_path):
        chat_path = str(tmp_path / "chat.json")
        with open(chat_path, "w", encoding="utf-8") as fh:
            json.dump([], fh)
        pane = client_tui_captures.CapsPane(chat_file=chat_path)
        pane.load_messages()
        assert pane.messages == []
        assert pane.channels == []


class TestChannelLabels:
    """CapsPane.channel_labels returns all-prefixed list."""

    def test_with_channels(self):
        pane = client_tui_captures.CapsPane(chat_file="")
        pane.channels = ["gossip", "ooc"]
        assert pane.channel_labels() == ["all", "gossip", "ooc"]

    def test_empty_channels(self):
        pane = client_tui_captures.CapsPane(chat_file="")
        pane.channels = []
        assert pane.channel_labels() == ["all"]


class TestActiveFilter:
    """CapsPane.active_filter channel selection."""

    def test_index_zero_returns_empty(self):
        pane = client_tui_captures.CapsPane(chat_file="")
        pane.channels = ["gossip", "ooc"]
        pane.filter_idx = 0
        assert pane.active_filter() == ""

    def test_index_one_returns_first_channel(self):
        pane = client_tui_captures.CapsPane(chat_file="")
        pane.channels = ["gossip", "ooc"]
        pane.filter_idx = 1
        assert pane.active_filter() == "gossip"

    def test_index_out_of_range_returns_empty(self):
        pane = client_tui_captures.CapsPane(chat_file="")
        pane.channels = ["gossip"]
        pane.filter_idx = 99
        assert pane.active_filter() == ""
