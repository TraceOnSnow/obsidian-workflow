"""A local Markdown library; construction never initializes a Vault."""

from .core import Library, LibraryError

__all__ = ["Library", "LibraryError"]
