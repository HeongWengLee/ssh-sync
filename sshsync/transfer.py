"""Transfer utilities for uploads/downloads with resume support."""

from __future__ import annotations

import logging
import os
import posixpath
import errno
from pathlib import Path, PurePosixPath

from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeRemainingColumn

from sshsync.ssh_connection import SSHConnection
from sshsync.utils import ensure_parent


LOGGER = logging.getLogger(__name__)


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
    resume = existing_size <= remote_size
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
    resume = remote_size <= local_size
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

