"""Transfer utilities for uploads/downloads with resume support."""

from __future__ import annotations

import logging
import os
import posixpath
import errno
import hashlib
from pathlib import Path, PurePosixPath

from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeRemainingColumn

from sshsync.ssh_connection import SSHConnection
from sshsync.utils import ensure_parent


LOGGER = logging.getLogger(__name__)
SAMPLE_SIZE = 64 * 1024


def _sha256_prefix_local(path: Path, size: int = SAMPLE_SIZE) -> str:
    """Return SHA256 of first `size` bytes from local file."""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        hasher.update(handle.read(size))
    return hasher.hexdigest()


def _sha256_prefix_remote(sftp, remote_path: str, size: int = SAMPLE_SIZE) -> str:
    """Return SHA256 of first `size` bytes from remote file."""
    hasher = hashlib.sha256()
    with sftp.open(remote_path, "rb") as handle:
        hasher.update(handle.read(size))
    return hasher.hexdigest()


def _progress() -> Progress:
    return Progress(
        TextColumn("{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
    )


def _ensure_remote_dirs(sftp, remote_path: str) -> None:
    """Create remote parent directories recursively if missing."""
    directory = posixpath.dirname(remote_path)
    if not directory:
        return

    segments = directory.split("/")
    current = "/" if remote_path.startswith("/") else ""
    for seg in segments:
        if not seg:
            continue
        current = posixpath.join(current, seg)
        try:
            sftp.stat(current)
        except OSError as exc:
            if getattr(exc, "errno", None) in (None, errno.ENOENT):
                sftp.mkdir(current)
            else:
                raise


def download_file(
    connection: SSHConnection,
    remote_root: str,
    local_root: Path,
    relative_path: PurePosixPath,
    dry_run: bool,
) -> None:
    """Download one file from remote, resuming existing partial local file."""
    if connection.sftp is None:
        raise RuntimeError("SFTP session is not connected")

    sftp = connection.sftp
    remote_path = posixpath.join(remote_root.rstrip("/"), relative_path.as_posix())
    local_path = local_root / Path(relative_path.as_posix())

    if dry_run:
        return

    ensure_parent(local_path)
    remote_size = int(sftp.stat(remote_path).st_size)
    existing_size = local_path.stat().st_size if local_path.exists() else 0
    local_mtime = local_path.stat().st_mtime if local_path.exists() else 0.0
    remote_mtime = float(sftp.stat(remote_path).st_mtime)
    resume = existing_size <= remote_size

    if resume and existing_size > 0:
        local_prefix = _sha256_prefix_local(local_path)
        remote_prefix = _sha256_prefix_remote(sftp, remote_path)
        if local_prefix != remote_prefix:
            resume = False

    offset = existing_size if resume else 0
    mode = "ab" if offset > 0 else "wb"

    with _progress() as progress:
        task = progress.add_task(f"Downloading {relative_path.as_posix()}", total=remote_size)
        progress.update(task, completed=offset)

        with sftp.open(remote_path, "rb") as r_handle:
            with local_path.open(mode) as l_handle:
                if offset > 0:
                    r_handle.seek(offset)
                while True:
                    chunk = r_handle.read(1024 * 1024)
                    if not chunk:
                        break
                    l_handle.write(chunk)
                    progress.advance(task, len(chunk))


def upload_file(
    connection: SSHConnection,
    local_root: Path,
    remote_root: str,
    relative_path: PurePosixPath,
    dry_run: bool,
) -> None:
    """Upload one file to remote, resuming when remote partial file exists."""
    if connection.sftp is None:
        raise RuntimeError("SFTP session is not connected")

    sftp = connection.sftp
    local_path = local_root / Path(relative_path.as_posix())
    remote_path = posixpath.join(remote_root.rstrip("/"), relative_path.as_posix())

    if dry_run:
        return

    _ensure_remote_dirs(sftp, remote_path)

    local_size = os.path.getsize(local_path)
    try:
        remote_size = int(sftp.stat(remote_path).st_size)
    except OSError as exc:
        if getattr(exc, "errno", None) in (None, errno.ENOENT):
            remote_size = 0
        else:
            raise
    local_mtime = os.path.getmtime(local_path)
    remote_mtime = 0.0
    if remote_size > 0:
        remote_mtime = float(sftp.stat(remote_path).st_mtime)

    resume = remote_size <= local_size
    if resume and remote_size > 0:
        if local_mtime > remote_mtime:
            resume = False
        else:
            local_prefix = _sha256_prefix_local(local_path)
            remote_prefix = _sha256_prefix_remote(sftp, remote_path)
            if local_prefix != remote_prefix:
                resume = False

    offset = remote_size if resume else 0
    mode = "ab" if offset > 0 else "wb"

    with _progress() as progress:
        task = progress.add_task(f"Uploading {relative_path.as_posix()}", total=local_size)
        progress.update(task, completed=offset)

        with local_path.open("rb") as l_handle:
            with sftp.open(remote_path, mode) as r_handle:
                if offset > 0:
                    l_handle.seek(offset)
                while True:
                    chunk = l_handle.read(1024 * 1024)
                    if not chunk:
                        break
                    r_handle.write(chunk)
                    progress.advance(task, len(chunk))

