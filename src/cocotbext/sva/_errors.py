"""Exceptions raised while building assertions."""
from __future__ import annotations


class SvaError(Exception):
    """Base class for errors in assertion text or bindings."""


class SvaSyntaxError(SvaError):
    """Malformed SVA text. The message shows the text with a caret under the error."""

    def __init__(self, message: str, text: str = "", pos: int = 0, end: int | None = None):
        self.raw_message = message
        self.text = text
        self.pos = pos
        if text:
            width = max(1, (end if end is not None else pos + 1) - pos)
            message = f"{message}\n    {text}\n    {' ' * pos}{'^' * width}"
        super().__init__(message)


class SvaNameError(SvaError):
    """A name in the assertion text could not be resolved."""


class SvaUnsupportedError(SvaError):
    """Valid SVA that this version does not support yet."""
