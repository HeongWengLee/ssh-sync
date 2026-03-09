"""Metadata models used by sync operations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal


PathKind = Literal["file", "directory"]


@dataclass(slots=True)
class FileMetadata:
    """Container for local or remote filesystem entry metadata."""

    relative_path: PurePosixPath
    kind: PathKind
    size: int
    mtime: float
    sha256: str | None = None

    @property
    def is_file(self) -> bool:
        """Return True when the metadata points to a file."""
        return self.kind == "file"

    @property
    def is_directory(self) -> bool:
        """Return True when the metadata points to a directory."""
        return self.kind == "directory"

