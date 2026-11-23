"""The two errors gqlparser raises.

They are siblings, not parent and child: "not valid GQL" and "valid GQL this
release does not support" are different answers, and a caller must be able to
tell them apart without inspecting the message.
"""


class GqlError(Exception):
    """Base for gqlparser errors. Never raised itself."""

    def __init__(self, message: str, line: int, column: int):
        super().__init__(f"{message} (line {line}, column {column})")
        self.message = message
        self.line = line
        self.column = column


class GqlSyntaxError(GqlError):
    """The text is not valid GQL."""


class GqlNotSupportedError(GqlError):
    """The text uses a construct outside the subset this release supports."""
