"""Kimi Sandbox: a filesystem sandbox launcher for Kimi Code.

This package builds and runs a bubblewrap command that confines the Kimi Code
CLI to a restricted filesystem view: the current project mounted read-write at
``/workspace``, an isolated ``KIMI_CODE_HOME``, read-only system directories, and
tmpfs ``HOME`` / ``/tmp``. A TIOCSTI-blocking seccomp filter is installed by
default; network isolation, read-only mode, cgroup resource limits, a
persistent cache, and extra mounts are opt-in. See the design document and
README for the full security model and its explicit non-goals.
"""

__version__ = "0.2.0"

__all__ = ["__version__"]
