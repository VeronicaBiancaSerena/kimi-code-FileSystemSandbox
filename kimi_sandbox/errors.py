"""Error types for kimi-sandbox.

The hierarchy mirrors design section 27. ``SandboxError`` carries an optional
``hint`` with actionable, user-readable remediation text. The CLI prints
``error: <message>`` followed by the hint (if any) and exits non-zero, so that
launcher failures are clearly distinguishable from Kimi's own exit codes.
"""

from __future__ import annotations


class SandboxError(Exception):
    """Base class for all launcher errors.

    ``message`` is a short one-line summary. ``hint`` is optional multi-line
    remediation guidance shown beneath the error.
    """

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class MissingDependencyError(SandboxError):
    """A required external dependency (e.g. bubblewrap) is not available."""


class InvalidProjectError(SandboxError):
    """The requested project directory is missing, not a dir, or disallowed."""


class KimiNotFoundError(SandboxError):
    """The Kimi executable could not be located or is not executable."""


class InvalidPathError(SandboxError):
    """A path relationship or sandbox-home target violates a safety rule."""


class BubblewrapFailedError(SandboxError):
    """bubblewrap itself failed to start the sandbox (not Kimi's exit code)."""


__all__ = [
    "SandboxError",
    "MissingDependencyError",
    "InvalidProjectError",
    "KimiNotFoundError",
    "InvalidPathError",
    "BubblewrapFailedError",
]
