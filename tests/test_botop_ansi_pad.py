"""Tests for ANSI-aware padding in botop.py — ticket CB-1417909-2565.

When colors are enabled, Python format-width specifiers count invisible
ANSI escape bytes, causing table columns to misalign. These tests verify
that _visible_width() and _ansi_pad() correctly handle this.
"""
from __future__ import annotations

import re
import sys
import os

# Ensure codebot package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from codebot.botop import _c, _strip_ansi, _visible_width, _ansi_pad


class TestStripAnsi:
    """Existing _strip_ansi should remove all ANSI escape sequences."""

    def test_plain_text_unchanged(self):
        assert _strip_ansi("hello") == "hello"

    def test_strips_color_codes(self):
        colored = _c("hello", "red", True)
        assert _strip_ansi(colored) == "hello"

    def test_strips_bold(self):
        bolded = _c("hello", "bold", True)
        assert _strip_ansi(bolded) == "hello"

    def test_empty_string(self):
        assert _strip_ansi("") == ""

    def test_nested_codes(self):
        # Simulate double-wrapped
        inner = _c("hello", "red", True)
        outer = _c(inner, "bold", True)
        assert _strip_ansi(outer) == "hello"


class TestVisibleWidth:
    """_visible_width should return the length of visible characters only."""

    def test_plain_text(self):
        assert _visible_width("hello") == 5

    def test_empty_string(self):
        assert _visible_width("") == 0

    def test_colored_text_returns_visible_length(self):
        colored = _c("RUNNING", "green", True)
        # Should be 7 (length of "RUNNING"), not the longer ANSI-wrapped string
        assert _visible_width(colored) == 7

    def test_multiple_colored_segments(self):
        text = _c("hi", "red", True) + " " + _c("there", "green", True)
        # visible: "hi there" = 8
        assert _visible_width(text) == 8

    def test_no_color_returns_same_as_len(self):
        plain = "hello world"
        assert _visible_width(plain) == len(plain)

    def test_various_color_names(self):
        for color in ["red", "green", "yellow", "blue", "magenta", "cyan", "gray", "bold", "dim"]:
            colored = _c("test", color, True)
            assert _visible_width(colored) == 4, f"Failed for color={color}"


class TestAnsiPad:
    """_ansi_pad should pad based on visible width, not raw string length."""

    def test_left_pad_shorter(self):
        """Left-aligned pad should add spaces after the visible text."""
        colored = _c("hi", "green", True)  # visible width 2
        result = _ansi_pad(colored, 10, align="left")
        visible = _strip_ansi(result)
        assert visible == "hi        "
        assert len(visible) == 10

    def test_left_pad_exact(self):
        colored = _c("hello", "red", True)  # visible width 5
        result = _ansi_pad(colored, 5, align="left")
        assert _strip_ansi(result) == "hello"

    def test_left_pad_no_truncation(self):
        """When text is wider than pad width, return as-is (no truncation)."""
        colored = _c("toolongtext", "red", True)  # visible width 11
        result = _ansi_pad(colored, 5, align="left")
        assert _strip_ansi(result) == "toolongtext"

    def test_right_pad_shorter(self):
        """Right-aligned pad should add spaces before the visible text."""
        colored = _c("hi", "green", True)
        result = _ansi_pad(colored, 10, align="right")
        visible = _strip_ansi(result)
        assert visible == "        hi"
        assert len(visible) == 10

    def test_right_pad_exact(self):
        colored = _c("hello", "yellow", True)
        result = _ansi_pad(colored, 5, align="right")
        assert _strip_ansi(result) == "hello"

    def test_plain_text_unchanged(self):
        result = _ansi_pad("hi", 10, align="left")
        assert result == "hi        "
        assert len(result) == 10

    def test_align_defaults_to_left(self):
        colored = _c("hi", "cyan", True)
        result = _ansi_pad(colored, 10)
        assert _strip_ansi(result) == "hi        "

    def test_color_codes_not_counted_in_width(self):
        """The critical test: colored string with narrow visible text should
        produce the same visible padding as plain text."""
        colored = _c("RUN", "green", True)  # visible: "RUN" = 3, raw: ~11
        plain = "RUN"
        pad_w = 10

        colored_result = _ansi_pad(colored, pad_w, align="left")
        plain_result = _ansi_pad(plain, pad_w, align="left")

        # Both should produce identical visible output
        assert _strip_ansi(colored_result) == plain_result

    def test_columns_align_with_different_colors(self):
        """Simulates real table: two rows with different color lengths should
        produce same visible column width."""
        green = _ansi_pad(_c("RUNNING", "green", True), 18, align="left")
        red = _ansi_pad(_c("DEAD", "red", True), 18, align="left")

        # Both should have visible width of exactly 18
        assert len(_strip_ansi(green)) == 18
        assert len(_strip_ansi(red)) == 18

    def test_right_align_columns_align(self):
        """Right-aligned colored values should also align."""
        a = _ansi_pad(_c("12s", "green", True), 15, align="right")
        b = _ansi_pad(_c("5m", "yellow", True), 15, align="right")
        assert len(_strip_ansi(a)) == 15
        assert len(_strip_ansi(b)) == 15

    def test_simulated_status_table_alignment(self):
        """Simulate what a botop status table row looks like with colors."""
        # Two agents with different buckets and hb ages
        bucket1 = _ansi_pad(_c("RUNNING", "green", True), 18, align="left")
        bucket2 = _ansi_pad(_c("DEAD", "red", True), 18, align="left")
        hb1 = _ansi_pad(_c("3s", "green", True), 15, align="right")
        hb2 = _ansi_pad(_c("2.3h", "red", True), 15, align="right")

        # Construct full rows
        row1 = f"  agent-1   {bucket1} {hb1}"
        row2 = f"  agent-2   {bucket2} {hb2}"

        # Check visible alignment
        vis1 = _strip_ansi(row1)
        vis2 = _strip_ansi(row2)

        # The "State" columns should end at the same position
        state_end = vis1.index(vis1[vis1.index("agent-1") + 9 + 18 + 1:])
        # Simpler check: visible lengths of the formatted sub-columns
        assert len(_strip_ansi(bucket1)) == 18
        assert len(_strip_ansi(bucket2)) == 18
        assert len(_strip_ansi(hb1)) == 15
        assert len(_strip_ansi(hb2)) == 15
