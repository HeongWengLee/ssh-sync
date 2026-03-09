"""Utility helpers for logging, hashing, and path handling."""

from __future__ import annotations

import fnmatch
import hashlib
import logging
from pathlib import Path, PurePosixPath
from typing import Iterable


DEFAULT_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def configure_logging(verbose: bool = False) -> None:
    """Configure root logging once for CLI execution."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format=DEFAULT_LOG_FORMAT)


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Compute SHA256 hash for a local file path."""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def normalize_relative_path(path: Path, root: Path) -> PurePosixPath:
    """Normalize local path to a POSIX-style relative path."""
    rel = path.absolute().relative_to(root.absolute())
    return PurePosixPath(rel.as_posix())


def ensure_parent(path: Path) -> None:
    """Create parent directories for path when needed."""
    path.parent.mkdir(parents=True, exist_ok=True)


def match_ignore_patterns(relative_path: PurePosixPath, patterns: Iterable[str]) -> bool:
    """Return True when path matches any ignore pattern."""
    rel_str = relative_path.as_posix()
    for pattern in patterns:
        if fnmatch.fnmatch(rel_str, pattern) or fnmatch.fnmatch(relative_path.name, pattern):
            return True
        if pattern.endswith("/") and rel_str.startswith(pattern.rstrip("/")):
            return True
    return False

