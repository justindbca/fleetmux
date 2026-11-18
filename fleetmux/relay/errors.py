"""Errors shared by every relay module."""


class ConfigError(ValueError):
    """A parameter or argument problem found at start (the built-in applet exits 78)."""


class RecordError(ValueError):
    """A commit that does not hold a valid record."""


class TransitionError(ValueError):
    """A record change that its current state does not allow."""
