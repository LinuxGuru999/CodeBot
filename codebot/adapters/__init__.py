"""Project adapter implementations for CodeBot portability validation.

This package contains concrete ProjectAdapter implementations that demonstrate
CodeBot's ability to operate on diverse codebases without project-specific
core modifications.

Available adapters:
- FlaskAppAdapter: Reference adapter for a generic Flask web application
- CodeBotAdapter: Self-hosting adapter (in codebot/codebot_adapter.py)
"""

from codebot.adapters.flask_app_adapter import FlaskAppAdapter

__all__ = ["FlaskAppAdapter"]
