"""Diff and conflict detection logic for sync planning."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from sshsync.metadata import FileMetadata


@dataclass(slots=True)
class SyncPlan:
    """Actions to perform for a sync run."""

    upload: list[PurePosixPath] = field(default_factory=list)
    download: list[PurePosixPath] = field(default_factory=list)
    conflicts: list[PurePosixPath] = field(default_factory=list)
    skip: list[PurePosixPath] = field(default_factory=list)


def _is_different(local: FileMetadata, remote: FileMetadata, use_hash: bool) -> bool:
    """Compare file metadata using size/mtime and optional hash."""
    if local.size != remote.size:
        return True

    mtime_equal = math.isclose(local.mtime, remote.mtime, abs_tol=1e-3)
    if not mtime_equal:
        if use_hash and local.sha256 and remote.sha256:
            return local.sha256 != remote.sha256
        return True

    if use_hash and local.sha256 and remote.sha256:
        return local.sha256 != remote.sha256
    return False


def _plan_by_newer_mtime(plan: SyncPlan, path: PurePosixPath, local_meta: FileMetadata, remote_meta: FileMetadata) -> None:
    """Fallback strategy: choose newer side when baseline is unavailable."""
    if math.isclose(local_meta.mtime, remote_meta.mtime, abs_tol=1e-3):
        plan.conflicts.append(path)
        return
    if local_meta.mtime > remote_meta.mtime:
        plan.upload.append(path)
    else:
        plan.download.append(path)


def build_pull_plan(
    local_entries: dict[PurePosixPath, FileMetadata],
    remote_entries: dict[PurePosixPath, FileMetadata],
    use_hash: bool,
) -> SyncPlan:
    """Create transfer plan for pull mode."""
    plan = SyncPlan()
    for path, remote_meta in remote_entries.items():
        if remote_meta.is_directory:
            continue
        local_meta = local_entries.get(path)
        if local_meta is None:
            plan.download.append(path)
        elif local_meta.is_file and _is_different(local_meta, remote_meta, use_hash):
            plan.download.append(path)
        else:
            plan.skip.append(path)
    return plan


def build_push_plan(
    local_entries: dict[PurePosixPath, FileMetadata],
    remote_entries: dict[PurePosixPath, FileMetadata],
    use_hash: bool,
) -> SyncPlan:
    """Create transfer plan for push mode."""
    plan = SyncPlan()
    for path, local_meta in local_entries.items():
        if local_meta.is_directory:
            continue
        remote_meta = remote_entries.get(path)
        if remote_meta is None:
            plan.upload.append(path)
        elif remote_meta.is_file and _is_different(local_meta, remote_meta, use_hash):
            plan.upload.append(path)
        else:
            plan.skip.append(path)
    return plan


def build_sync_plan(
    local_entries: dict[PurePosixPath, FileMetadata],
    remote_entries: dict[PurePosixPath, FileMetadata],
    use_hash: bool,
    last_sync_timestamp: float | None = None,
) -> SyncPlan:
    """Create transfer/conflict plan for bidirectional sync mode."""
    plan = SyncPlan()
    all_paths = sorted(set(local_entries) | set(remote_entries))

    for path in all_paths:
        local_meta = local_entries.get(path)
        remote_meta = remote_entries.get(path)

        if local_meta and local_meta.is_directory:
            continue
        if remote_meta and remote_meta.is_directory:
            continue

        if local_meta is None and remote_meta is not None:
            plan.download.append(path)
            continue
        if remote_meta is None and local_meta is not None:
            plan.upload.append(path)
            continue
        if local_meta is None or remote_meta is None:
            continue

        if not _is_different(local_meta, remote_meta, use_hash):
            plan.skip.append(path)
            continue

        if last_sync_timestamp is not None:
            local_changed = local_meta.mtime > last_sync_timestamp
            remote_changed = remote_meta.mtime > last_sync_timestamp

            if local_changed and remote_changed:
                plan.conflicts.append(path)
            elif local_changed:
                plan.upload.append(path)
            elif remote_changed:
                plan.download.append(path)
            else:
                _plan_by_newer_mtime(plan, path, local_meta, remote_meta)
            continue

        _plan_by_newer_mtime(plan, path, local_meta, remote_meta)

    return plan
