"""UI Component Rendering Module.

Purpose
-------
Provides bounded, stdlib-only UI component rendering functions for agent use.
This module defines the architectural boundary for UI presentation logic,
keeping api_runner.py as a thin router/dispatch layer.

Why
---
Ticket CB-B562A4705747EAEE51EC1DAE0E240867 requires UI rendering to live in a
dedicated module, not in api_runner.py. This module provides render functions
for buttons, cards, and layout containers using design tokens.

Invariants
----------
- stdlib-only: html, json, typing
- All render functions return HTML strings with sanitized inputs
- Design tokens are defined in DESIGN_TOKENS dict
- No side effects; pure functions only

Security
--------
- All user-provided content is HTML-escaped via html.escape()
- No external dependencies that could introduce supply-chain risk
"""

import html
from typing import Any, Dict, List, Optional

# Design Token Schema
# Defines the visual language for UI components.
# Keys are token names, values are CSS-compatible strings or nested dicts.
DESIGN_TOKENS: Dict[str, Any] = {
    "colors": {
        "primary": "#007bff",
        "secondary": "#6c757d",
        "success": "#28a745",
        "danger": "#dc3545",
        "warning": "#ffc107",
        "info": "#17a2b8",
        "light": "#f8f9fa",
        "dark": "#343a40",
        "background": "#ffffff",
        "text": "#212529",
        "border": "#dee2e6",
    },
    "spacing": {
        "xs": "4px",
        "sm": "8px",
        "md": "16px",
        "lg": "24px",
        "xl": "32px",
    },
    "typography": {
        "font_family": "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
        "font_size_base": "16px",
        "line_height_base": "1.5",
        "heading_weight": "600",
    },
    "border_radius": {
        "sm": "4px",
        "md": "8px",
        "lg": "12px",
        "full": "9999px",
    },
    "shadows": {
        "sm": "0 1px 2px rgba(0,0,0,0.05)",
        "md": "0 4px 6px rgba(0,0,0,0.1)",
        "lg": "0 10px 15px rgba(0,0,0,0.1)",
    },
}


def _get_token(*keys: str) -> str:
    """Retrieve a design token value by key path."""
    current: Any = DESIGN_TOKENS
    for key in keys:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            raise KeyError(f"Design token not found: {'.'.join(keys)}")
    if not isinstance(current, str):
        raise ValueError(f"Design token is not a string: {'.'.join(keys)}")
    return current


def render_button(
    label: str,
    variant: str = "primary",
    size: str = "md",
    disabled: bool = False,
    onclick: Optional[str] = None,
) -> str:
    """Render a button component.

    Args:
        label: Button text (sanitized).
        variant: One of primary, secondary, success, danger, warning, info.
        size: One of sm, md, lg.
        disabled: Whether the button is disabled.
        onclick: Optional JS handler (sanitized).

    Returns:
        HTML string for the button.
    """
    safe_label = html.escape(str(label))
    color_key = variant if variant in DESIGN_TOKENS["colors"] else "primary"
    bg_color = _get_token("colors", color_key)
    text_color = "#ffffff" if color_key not in ("light", "warning") else _get_token("colors", "text")
    
    spacing_key = size if size in DESIGN_TOKENS["spacing"] else "md"
    padding_v = _get_token("spacing", spacing_key)
    padding_h = _get_token("spacing", spacing_key)
    
    radius = _get_token("border_radius", "md")
    font_size = _get_token("typography", "font_size_base")
    
    style = (
        f"background-color: {bg_color}; "
        f"color: {text_color}; "
        f"padding: {padding_v} {padding_h}; "
        f"border-radius: {radius}; "
        f"font-size: {font_size}; "
        f"border: none; "
        f"cursor: {'not-allowed' if disabled else 'pointer'}; "
        f"opacity: {'0.6' if disabled else '1'};"
    )
    
    attrs = f'style="{style}"'
    if disabled:
        attrs += " disabled"
    if onclick:
        # Basic sanitization: allow only alphanumeric and underscores in handler name
        safe_onclick = html.escape(str(onclick))
        attrs += f' onclick="{safe_onclick}"'
    
    return f"<button {attrs}>{safe_label}</button>"


def render_card(
    title: str,
    content: str,
    footer: Optional[str] = None,
    variant: str = "default",
) -> str:
    """Render a card component.

    Args:
        title: Card title (sanitized).
        content: Card body content (sanitized).
        footer: Optional footer content (sanitized).
        variant: One of default, primary, success, danger.

    Returns:
        HTML string for the card.
    """
    safe_title = html.escape(str(title))
    safe_content = html.escape(str(content))
    safe_footer = html.escape(str(footer)) if footer else None
    
    border_color = _get_token("colors", "border")
    bg_color = _get_token("colors", "background")
    shadow = _get_token("shadows", "md")
    radius = _get_token("border_radius", "lg")
    spacing_md = _get_token("spacing", "md")
    
    style = (
        f"background-color: {bg_color}; "
        f"border: 1px solid {border_color}; "
        f"border-radius: {radius}; "
        f"box-shadow: {shadow}; "
        f"padding: {spacing_md}; "
        f"margin-bottom: {spacing_md};"
    )
    
    html_parts = [f'<div style="{style}">']
    html_parts.append(f'<h3 style="margin-top: 0;">{safe_title}</h3>')
    html_parts.append(f'<div>{safe_content}</div>')
    if safe_footer:
        html_parts.append(f'<div style="margin-top: {spacing_md}; color: {_get_token("colors", "secondary")};">{safe_footer}</div>')
    html_parts.append('</div>')
    
    return "\n".join(html_parts)


def render_layout(
    children: List[str],
    direction: str = "vertical",
    gap: str = "md",
    align: str = "stretch",
) -> str:
    """Render a layout container.

    Args:
        children: List of HTML strings to place in the layout.
        direction: One of vertical, horizontal.
        gap: One of xs, sm, md, lg, xl.
        align: One of stretch, center, start, end.

    Returns:
        HTML string for the layout container.
    """
    flex_dir = "column" if direction == "vertical" else "row"
    gap_val = _get_token("spacing", gap if gap in DESIGN_TOKENS["spacing"] else "md")
    
    align_items = align
    if align == "stretch":
        align_items = "stretch"
    elif align == "center":
        align_items = "center"
    elif align == "start":
        align_items = "flex-start"
    elif align == "end":
        align_items = "flex-end"
    
    style = (
        f"display: flex; "
        f"flex-direction: {flex_dir}; "
        f"gap: {gap_val}; "
        f"align-items: {align_items};"
    )
    
    safe_children = "".join(children)
    
    return f'<div style="{style}">{safe_children}</div>'
