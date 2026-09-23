"""Tests for codebot/ui_components.py.

Verifies:
- Module is importable
- DESIGN_TOKENS schema exists and is valid
- render_button, render_card, render_layout functions exist and return sanitized HTML
"""
import pytest
from codebot.ui_components import (
    DESIGN_TOKENS,
    render_button,
    render_card,
    render_layout,
    _get_token,
)


class TestDesignTokens:
    def test_design_tokens_exists(self):
        assert isinstance(DESIGN_TOKENS, dict)
        assert "colors" in DESIGN_TOKENS
        assert "spacing" in DESIGN_TOKENS
        assert "typography" in DESIGN_TOKENS
        assert "border_radius" in DESIGN_TOKENS
        assert "shadows" in DESIGN_TOKENS

    def test_get_token_valid_path(self):
        assert _get_token("colors", "primary") == "#007bff"
        assert _get_token("spacing", "md") == "16px"

    def test_get_token_invalid_path_raises(self):
        with pytest.raises(KeyError):
            _get_token("nonexistent", "key")

    def test_get_token_non_string_value_raises(self):
        with pytest.raises(ValueError):
            _get_token("colors")  # Returns a dict, not a string


class TestRenderButton:
    def test_render_button_basic(self):
        html = render_button("Click Me")
        assert "<button" in html
        assert "Click Me" in html
        assert "style=" in html

    def test_render_button_sanitizes_label(self):
        html = render_button("<script>alert('xss')</script>")
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_render_button_disabled(self):
        html = render_button("Disabled", disabled=True)
        assert "disabled" in html
        assert "not-allowed" in html

    def test_render_button_variant(self):
        html = render_button("Danger", variant="danger")
        assert "#dc3545" in html  # danger color

    def test_render_button_onclick_sanitized(self):
        html = render_button("Btn", onclick="do_something()")
        assert 'onclick="do_something()"' in html


class TestRenderCard:
    def test_render_card_basic(self):
        html = render_card("Title", "Content")
        assert "<div" in html
        assert "Title" in html
        assert "Content" in html

    def test_render_card_sanitizes_content(self):
        html = render_card("<b>Title</b>", "<script>alert('xss')</script>")
        assert "<b>" not in html
        assert "<script>" not in html
        assert "&lt;b&gt;Title&lt;/b&gt;" in html

    def test_render_card_with_footer(self):
        html = render_card("Title", "Content", footer="Footer Text")
        assert "Footer Text" in html


class TestRenderLayout:
    def test_render_layout_vertical(self):
        html = render_layout(["<div>Child 1</div>", "<div>Child 2</div>"])
        assert "flex-direction: column" in html
        assert "Child 1" in html
        assert "Child 2" in html

    def test_render_layout_horizontal(self):
        html = render_layout(["<div>A</div>"], direction="horizontal")
        assert "flex-direction: row" in html

    def test_render_layout_gap(self):
        html = render_layout([], gap="lg")
        assert "gap: 24px" in html  # lg spacing

    def test_render_layout_align(self):
        html = render_layout([], align="center")
        assert "align-items: center" in html

    def test_render_layout_align_start(self):
        html = render_layout([], align="start")
        assert "align-items: flex-start" in html

    def test_render_layout_align_end(self):
        html = render_layout([], align="end")
        assert "align-items: flex-end" in html

    def test_render_layout_align_stretch(self):
        html = render_layout([], align="stretch")
        assert "align-items: stretch" in html
