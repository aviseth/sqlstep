"""Plain SQL migrations, without an ORM.

The public surface is the ``sqlstep`` command. Nothing is imported at module
scope so that a driver you do not use is never loaded.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
