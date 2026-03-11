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


def _sha256_full_local(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Return SHA256 of full local file."""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _sha256_full_remote(sftp, remote_path: str, chunk_size: int = 1024 * 1024) -> str:
    """Return SHA256 of full remote file over SFTP."""
    hasher = hashlib.sha256()
    with sftp.open(remote_path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
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
    verify: bool = False,
) -> None:
    """Download one file from remote, resuming existing partial local file."""
    if connection.sftp is None:
        raise RuntimeError("SFTP session is not connected")

    sftp = connection.sftp
    remote_path = posixpath.join(remote_root.rstrip("/"), relative_path.as_posix())
    local_path = local_root / Path(relative_path.as_posix())
    tmp_path = local_path.with_name(f"{local_path.name}.tmp")

    if dry_run:
        return

    ensure_parent(local_path)
    remote_stat = sftp.stat(remote_path)
    remote_size = int(remote_stat.st_size)
    existing_size = tmp_path.stat().st_size if tmp_path.exists() else 0
    resume = existing_size <= remote_size

    if resume and existing_size > 0:
        local_prefix = _sha256_prefix_local(tmp_path)
        remote_prefix = _sha256_prefix_remote(sftp, remote_path)
        if local_prefix != remote_prefix:
            resume = False

    offset = existing_size if resume else 0
    mode = "ab" if offset > 0 else "wb"

    try:
        with _progress() as progress:
            task = progress.add_task(f"Downloading {relative_path.as_posix()}", total=remote_size)
            progress.update(task, completed=offset)

            with sftp.open(remote_path, "rb") as r_handle:
                with tmp_path.open(mode) as l_handle:
                    if offset > 0:
                        r_handle.seek(offset)
                    while True:
                        chunk = r_handle.read(1024 * 1024)
                        if not chunk:
                            break
                        l_handle.write(chunk)
                        progress.advance(task, len(chunk))

        if verify:
            local_digest = _sha256_full_local(tmp_path)
            remote_digest = _sha256_full_remote(sftp, remote_path)
            if local_digest != remote_digest:
                raise RuntimeError(
                    f"Checksum mismatch for downloaded file {relative_path.as_posix()} "
                    f"(local={local_digest}, remote={remote_digest})"
                )

        os.replace(tmp_path, local_path)
        os.chmod(local_path, int(remote_stat.st_mode) & 0o7777)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def upload_file(
    connection: SSHConnection,
    local_root: Path,
    remote_root: str,
    relative_path: PurePosixPath,
    dry_run: bool,
    copy_links: bool = False,
    verify: bool = False,
) -> None:
    """Upload one file to remote, resuming when remote partial file exists."""
    if connection.sftp is None:
        raise RuntimeError("SFTP session is not connected")

    sftp = connection.sftp
    local_path = local_root / Path(relative_path.as_posix())
    source_path = local_path
    if copy_links and local_path.is_symlink():
        source_path = local_path.resolve(strict=True)
    remote_path = posixpath.join(remote_root.rstrip("/"), relative_path.as_posix())

    if dry_run:
        return

    #_ensure_remote_dirs(sftp, remote_path)

    local_size = os.path.getsize(source_path)
    try:
        remote_size = int(sftp.stat(remote_path).st_size)
    except OSError as exc:
        if getattr(exc, "errno", None) in (None, errno.ENOENT):
            remote_size = 0
        else:
            raise
    local_mtime = os.path.getmtime(source_path)
    remote_mtime = 0.0
    if remote_size > 0:
        remote_mtime = float(sftp.stat(remote_path).st_mtime)

    resume = remote_size <= local_size
    if resume and remote_size > 0:
        if local_mtime > remote_mtime:
            resume = False
        else:
            local_prefix = _sha256_prefix_local(source_path)
            remote_prefix = _sha256_prefix_remote(sftp, remote_path)
            if local_prefix != remote_prefix:
                resume = False

    offset = remote_size if resume else 0
    mode = "ab" if offset > 0 else "wb"

    with _progress() as progress:
        task = progress.add_task(f"Uploading {relative_path.as_posix()}", total=local_size)
        progress.update(task, completed=offset)

        with source_path.open("rb") as l_handle:
            with sftp.open(remote_path, mode) as r_handle:
                if offset > 0:
                    l_handle.seek(offset)
                while True:
                    chunk = l_handle.read(1024 * 1024)
                    if not chunk:
                        break
                    r_handle.write(chunk)
                    progress.advance(task, len(chunk))

    local_mode = int(os.stat(source_path).st_mode) & 0o7777
    sftp.chmod(remote_path, local_mode)
    if verify:
        local_digest = _sha256_full_local(source_path)
        remote_digest = _sha256_full_remote(sftp, remote_path)
        if local_digest != remote_digest:
            raise RuntimeError(
                f"Checksum mismatch for uploaded file {relative_path.as_posix()} "
                f"(local={local_digest}, remote={remote_digest})"
            )

