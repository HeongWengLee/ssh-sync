"""Filesystem discovery for local and remote trees."""

from __future__ import annotations

import logging
import os
import posixpath
import shlex
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from sshsync.metadata import FileMetadata
from sshsync.ssh_connection import SSHConnection
from sshsync.utils import match_ignore_patterns, normalize_relative_path, sha256_file


LOGGER = logging.getLogger(__name__)
CONFLICT_SUFFIXES = (".local", ".remote")


@dataclass(slots=True)
class ScanSummary:
    """Scanner counters for one tree walk."""

    scanned_files: int = 0
    ignored_files: int = 0


def _remote_sha256(connection: SSHConnection, remote_path: str) -> str | None:
    """Compute remote SHA256 with tool fallbacks."""
    quoted = shlex.quote(remote_path)
    commands = [
        f"sha256sum {quoted}",
        f"shasum -a 256 {quoted}",
        f"openssl dgst -sha256 {quoted}",
    ]
    for cmd in commands:
        out, err, code = connection.exec_command(cmd)
        if code != 0:
            LOGGER.debug("Hash command failed for %s with %s: %s", remote_path, cmd, err.strip())
            continue
        text = out.strip()
        if not text:
            continue
        if "=" in text:
            return text.split("=")[-1].strip()
        return text.split()[0]
    return None


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


def _is_conflict_artifact(path: PurePosixPath) -> bool:
    """Return True if path is a sync conflict artifact file."""
    return path.name.endswith(CONFLICT_SUFFIXES)


def scan_local_tree(
    local_root: Path,
    use_hash: bool,
    ignore_patterns: list[str],
) -> tuple[dict[PurePosixPath, FileMetadata], ScanSummary]:
    """Recursively scan local tree and return metadata map and scan summary."""
    result: dict[PurePosixPath, FileMetadata] = {}
    summary = ScanSummary()
    root = local_root.expanduser().resolve()

    for current, dirnames, filenames in os.walk(root):
        current_path = Path(current)

        # Prune ignored directories
        pruned_dirs: list[str] = []
        for name in dirnames:
            rel = normalize_relative_path(current_path / name, root)
            if match_ignore_patterns(rel, ignore_patterns):
                summary.ignored_files += 1
                continue
            pruned_dirs.append(name)
            result[rel] = FileMetadata(relative_path=rel, kind="directory", size=0, mtime=(current_path / name).stat().st_mtime)
        dirnames[:] = pruned_dirs

        for filename in filenames:
            if filename == ".sync_state.json":
                continue

            full = current_path / filename
            summary.scanned_files += 1
            rel = normalize_relative_path(full, root)
            if match_ignore_patterns(rel, ignore_patterns):
                summary.ignored_files += 1
                continue
            if _is_conflict_artifact(rel):
                summary.ignored_files += 1
                continue
            st = full.lstat()
            if stat.S_ISLNK(st.st_mode):
                LOGGER.warning("Skipping local symlink: %s", rel.as_posix())
                summary.ignored_files += 1
                continue
            digest = sha256_file(full) if use_hash else None
            result[rel] = FileMetadata(
                relative_path=rel,
                kind="file",
                size=st.st_size,
                mtime=st.st_mtime,
                sha256=digest,
            )

    return result, summary


def scan_remote_tree(
    connection: SSHConnection,
    remote_root: str,
    use_hash: bool,
    ignore_patterns: list[str],
) -> tuple[dict[PurePosixPath, FileMetadata], ScanSummary]:
    """Recursively scan remote tree over SFTP and return metadata map and scan summary."""
    if connection.sftp is None:
        raise RuntimeError("SFTP session is not connected")

    sftp = connection.sftp
    result: dict[PurePosixPath, FileMetadata] = {}
    summary = ScanSummary()
    normalized_root = posixpath.normpath(remote_root)

    stack = [normalized_root]
    while stack:
        current = stack.pop()
        try:
            attrs = sftp.listdir_attr(current)
        except OSError as exc:
            LOGGER.warning("Cannot list remote directory %s: %s", current, exc)
            continue

        for attr in attrs:
            remote_path = posixpath.join(current, attr.filename)
            rel_str = posixpath.relpath(remote_path, normalized_root)
            rel = PurePosixPath(rel_str)

            if rel_str == ".":
                continue
            if match_ignore_patterns(rel, ignore_patterns):
                summary.ignored_files += 1
                continue
            if _is_conflict_artifact(rel):
                summary.ignored_files += 1
                continue

            if stat.S_ISLNK(attr.st_mode):
                LOGGER.warning("Skipping remote symlink: %s", remote_path)
                summary.ignored_files += 1
                continue

            if stat.S_ISDIR(attr.st_mode):
                result[rel] = FileMetadata(relative_path=rel, kind="directory", size=0, mtime=float(attr.st_mtime))
                stack.append(remote_path)
            elif stat.S_ISREG(attr.st_mode):
                summary.scanned_files += 1
                digest = None
                if use_hash:
                    digest = _remote_sha256(connection, remote_path)
                    if digest is None:
                        LOGGER.warning("Remote hash failed for %s", remote_path)

                result[rel] = FileMetadata(
                    relative_path=rel,
                    kind="file",
                    size=int(attr.st_size),
                    mtime=float(attr.st_mtime),
                    sha256=digest,
                )

    return result, summary
