"""Stable errors exposed by the CLI."""


class GitViewspaceError(Exception):
    """A validated operation failure with a stable public code.

    ``data`` carries bounded machine-readable diagnostics such as merge-conflict paths;
    ``partial_state`` is reserved for a mutation that left managed state behind.
    """

    def __init__(self, code, message, *, detail=None, data=None, partial_state=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
        self.data = data
        self.partial_state = partial_state


class UsageError(Exception):
    """Invalid command syntax."""


class InjectedFailure(RuntimeError):
    """Test-only interruption at a durable boundary."""
