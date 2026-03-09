"""Filesystem discovery for local and remote trees."""

from __future__ import annotations

import logging
import os
import posixpath
import shlex
import stat
from pathlib import Path, PurePosixPath

from sshsync.metadata import FileMetadata
from sshsync.ssh_connection import SSHConnection
from sshsync.utils import match_ignore_patterns, normalize_relative_path, sha256_file


LOGGER = logging.getLogger(__name__)


def load_ignore_patterns(local_root: Path) -> list[str]:
    """Load ignore patterns from `.syncignore` under the local root."""
    ignore_file = local_root / ".syncignore"
    patterns: list[str] = []
    if ignore_file.exists():
        for line in ignore_file.read_text(encoding="utf-8").splitlines():
            cleaned = line.strip()
            if cleaned and not cleaned.startswith("#"):
                patterns.append(cleaned)
    return patterns


def scan_local_tree(local_root: Path, use_hash: bool, ignore_patterns: list[str]) -> dict[PurePosixPath, FileMetadata]:
    """Recursively scan local tree and return normalized metadata map."""
    result: dict[PurePosixPath, FileMetadata] = {}
    root = local_root.expanduser().resolve()

    for current, dirnames, filenames in os.walk(root):
        current_path = Path(current)

        # Prune ignored directories
        pruned_dirs: list[str] = []
        for name in dirnames:
            rel = normalize_relative_path(current_path / name, root)
            if match_ignore_patterns(rel, ignore_patterns):
                continue
            pruned_dirs.append(name)
            result[rel] = FileMetadata(relative_path=rel, kind="directory", size=0, mtime=(current_path / name).stat().st_mtime)
        dirnames[:] = pruned_dirs

        for filename in filenames:
            full = current_path / filename
            rel = normalize_relative_path(full, root)
            if match_ignore_patterns(rel, ignore_patterns):
                continue
            st = full.stat()
            digest = sha256_file(full) if use_hash else None
            result[rel] = FileMetadata(
                relative_path=rel,
                kind="file",
                size=st.st_size,
                mtime=st.st_mtime,
                sha256=digest,
            )

    return result


def scan_remote_tree(
    connection: SSHConnection,
    remote_root: str,
    use_hash: bool,
    ignore_patterns: list[str],
) -> dict[PurePosixPath, FileMetadata]:
    """Recursively scan remote tree over SFTP and return metadata map."""
    if connection.sftp is None:
        raise RuntimeError("SFTP session is not connected")

    sftp = connection.sftp
    result: dict[PurePosixPath, FileMetadata] = {}
    normalized_root = posixpath.normpath(remote_root)

    stack = [normalized_root]
    while stack:
        current = stack.pop()
        for attr in sftp.listdir_attr(current):
            remote_path = posixpath.join(current, attr.filename)
            rel_str = posixpath.relpath(remote_path, normalized_root)
            rel = PurePosixPath(rel_str)

            if rel_str == "." or match_ignore_patterns(rel, ignore_patterns):
                continue

            if stat.S_ISDIR(attr.st_mode):
                result[rel] = FileMetadata(relative_path=rel, kind="directory", size=0, mtime=float(attr.st_mtime))
                stack.append(remote_path)
            elif stat.S_ISREG(attr.st_mode):
                digest = None
                if use_hash:
                    cmd = f"sha256sum {shlex.quote(remote_path)} | awk '{{print $1}}'"
                    out, err, code = connection.exec_command(cmd)
                    if code == 0:
                        digest = out.strip() or None
                    else:
                        LOGGER.warning("Remote hash failed for %s: %s", remote_path, err.strip())

                result[rel] = FileMetadata(
                    relative_path=rel,
                    kind="file",
                    size=int(attr.st_size),
                    mtime=float(attr.st_mtime),
                    sha256=digest,
                )

    return result

