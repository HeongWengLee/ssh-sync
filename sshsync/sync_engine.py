"""Main synchronization orchestrator."""

from __future__ import annotations

import concurrent.futures
import difflib
import json
import logging
import math
import posixpath
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from rich.console import Console

from sshsync.diff_engine import SyncPlan, build_pull_plan, build_push_plan, build_sync_plan
from sshsync.metadata import FileMetadata
from sshsync.scanner import ScanSummary, load_ignore_patterns, scan_local_tree, scan_remote_tree
from sshsync.ssh_connection import SSHConnection
from sshsync.transfer import download_file, upload_file
from sshsync.utils import ensure_parent


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class SyncResult:
    """Summary counters for one sync execution."""

    uploaded: list[PurePosixPath] = field(default_factory=list)
    downloaded: list[PurePosixPath] = field(default_factory=list)
    skipped: list[PurePosixPath] = field(default_factory=list)
    conflicts: list[PurePosixPath] = field(default_factory=list)
    skipped_ignored: int = 0
    deleted_local: list[PurePosixPath] = field(default_factory=list)
    deleted_remote: list[PurePosixPath] = field(default_factory=list)
    total_scanned: int = 0
    duration_seconds: float = 0.0


class SyncEngine:
    """Coordinates scanning, planning, conflict resolution, and transfer."""

    def __init__(
        self,
        connection: SSHConnection,
        local_dir: Path,
        remote_dir: str,
        use_hash: bool = False,
        dry_run: bool = False,
        delete_missing: bool = False,
        copy_links: bool = False,
        conflict_policy: str = "interactive",
        verify: bool = False,
        console: Console | None = None,
    ) -> None:
        self.connection = connection
        self.local_dir = local_dir.expanduser().resolve()
        self.remote_dir = posixpath.normpath(remote_dir)
        self.use_hash = use_hash
        self.dry_run = dry_run
        self.delete_missing = delete_missing
        self.copy_links = copy_links
        self.conflict_policy = conflict_policy
        self.verify = verify
        self.console = console or Console()
        self._transfer_errors = 0
        self._state_file = self.local_dir / ".sync_state.json"

    def run_pull(self) -> SyncResult:
        """Execute pull mode (remote -> local)."""
        self._transfer_errors = 0
        started = time.perf_counter()
        plan, _local_entries, _remote_entries, scan_summary = self._build_plan("pull")
        result = SyncResult(
            skipped_ignored=scan_summary.ignored_files,
            total_scanned=scan_summary.scanned_files,
        )
        self._apply_downloads(plan.download, result)
        self._apply_local_deletes(plan.delete_local, result)
        self._apply_remote_deletes(plan.delete_remote, result)
        self._report_skips(plan.skip)
        result.skipped.extend(plan.skip)
        self._report_ignored(scan_summary.ignored_files)

        if not self.dry_run and self._transfer_errors == 0:
            self._save_last_sync_timestamp(time.time())

        result.duration_seconds = time.perf_counter() - started
        return result

    def run_push(self) -> SyncResult:
        """Execute push mode (local -> remote)."""
        self._transfer_errors = 0
        started = time.perf_counter()
        plan, _local_entries, _remote_entries, scan_summary = self._build_plan("push")
        result = SyncResult(
            skipped_ignored=scan_summary.ignored_files,
            total_scanned=scan_summary.scanned_files,
        )
        self._apply_uploads(plan.upload, result)
        self._apply_local_deletes(plan.delete_local, result)
        self._apply_remote_deletes(plan.delete_remote, result)
        self._report_skips(plan.skip)
        result.skipped.extend(plan.skip)
        self._report_ignored(scan_summary.ignored_files)

        if not self.dry_run and self._transfer_errors == 0:
            self._save_last_sync_timestamp(time.time())

        result.duration_seconds = time.perf_counter() - started
        return result

    def run_sync(self) -> SyncResult:
        """Execute bidirectional sync mode."""
        self._transfer_errors = 0
        started = time.perf_counter()
        last_sync = self._load_last_sync_timestamp()
        plan, local_entries, remote_entries, scan_summary = self._build_plan("sync", last_sync)
        result = SyncResult(
            skipped_ignored=scan_summary.ignored_files,
            total_scanned=scan_summary.scanned_files,
        )

        self._apply_uploads(plan.upload, result)
        self._apply_downloads(plan.download, result)
        self._apply_local_deletes(plan.delete_local, result)
        self._apply_remote_deletes(plan.delete_remote, result)

        unresolved_conflict = False
        for path in plan.conflicts:
            result.conflicts.append(path)
            choice = self._resolve_conflict(path, local_entries, remote_entries)
            if choice == "remote":
                self._apply_downloads([path], result)
            elif choice == "local":
                self._apply_uploads([path], result)
            elif choice == "both":
                self._keep_both(path)
                result.downloaded.append(path)
                result.uploaded.append(path)
            elif choice == "skip":
                unresolved_conflict = True
                result.skipped.append(path)

        self._report_skips(plan.skip)
        result.skipped.extend(plan.skip)

        self._report_ignored(scan_summary.ignored_files)

        if not self.dry_run and self._transfer_errors == 0 and not unresolved_conflict:
            self._save_last_sync_timestamp(time.time())

        result.duration_seconds = time.perf_counter() - started
        return result

    def _build_plan(
        self,
        mode: str,
        last_sync_timestamp: float | None = None,
    ) -> tuple[SyncPlan, dict[PurePosixPath, FileMetadata], dict[PurePosixPath, FileMetadata], ScanSummary]:
        """Scan trees and compute plan for a specific mode."""
        ignore_patterns = load_ignore_patterns(self.local_dir)
        local_entries, local_summary = scan_local_tree(
            self.local_dir,
            self.use_hash,
            ignore_patterns,
            copy_links=self.copy_links,
        )
        remote_entries, remote_summary = scan_remote_tree(
            self.connection,
            self.remote_dir,
            self.use_hash,
            ignore_patterns,
            copy_links=self.copy_links,
        )
        scan_summary = ScanSummary(
            scanned_files=local_summary.scanned_files + remote_summary.scanned_files,
            ignored_files=local_summary.ignored_files + remote_summary.ignored_files,
        )

        if mode == "pull":
            plan = build_pull_plan(local_entries, remote_entries, self.use_hash, delete_missing=self.delete_missing)
        elif mode == "push":
            plan = build_push_plan(local_entries, remote_entries, self.use_hash, delete_missing=self.delete_missing)
        elif mode == "sync":
            plan = build_sync_plan(
                local_entries,
                remote_entries,
                self.use_hash,
                last_sync_timestamp=last_sync_timestamp,
                delete_missing=self.delete_missing,
            )
        else:
            raise ValueError(f"Unsupported mode: {mode}")

        return plan, local_entries, remote_entries, scan_summary

    def _apply_local_deletes(self, items: list[PurePosixPath], result: SyncResult) -> None:
        """Delete local files planned for removal."""
        for path in items:
            target = self.local_dir / Path(path.as_posix())
            if self.dry_run:
                self.console.print(f"- delete {path.as_posix()}")
                result.deleted_local.append(path)
                continue
            try:
                target.unlink(missing_ok=True)
                result.deleted_local.append(path)
            except OSError as exc:
                LOGGER.error("Local delete failed for %s: %s", path.as_posix(), exc)
                self._transfer_errors += 1
                result.skipped.append(path)

    def _apply_remote_deletes(self, items: list[PurePosixPath], result: SyncResult) -> None:
        """Delete remote files planned for removal."""
        if self.connection.sftp is None:
            raise RuntimeError("SFTP session is not connected")
        for path in items:
            remote_path = posixpath.join(self.remote_dir, path.as_posix())
            if self.dry_run:
                self.console.print(f"- delete {path.as_posix()}")
                result.deleted_remote.append(path)
                continue
            try:
                self.connection.sftp.remove(remote_path)
                result.deleted_remote.append(path)
            except OSError as exc:
                LOGGER.error("Remote delete failed for %s: %s", path.as_posix(), exc)
                self._transfer_errors += 1
                result.skipped.append(path)

    def _load_last_sync_timestamp(self) -> float | None:
        """Load last successful sync timestamp from local state file."""
        if not self._state_file.exists():
            return None
        try:
            payload = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOGGER.warning("Invalid sync state file, ignoring: %s", self._state_file)
            return None

        raw = payload.get("last_sync_timestamp")
        if isinstance(raw, (float, int)):
            return float(raw)
        return None

    def _save_last_sync_timestamp(self, timestamp: float) -> None:
        """Persist last successful sync timestamp to local state file."""
        try:
            self._state_file.write_text(
                json.dumps({"last_sync_timestamp": timestamp}, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            LOGGER.warning("Unable to write sync state file %s: %s", self._state_file, exc)

    def _is_binary_local(self, path: Path) -> bool:
        """Return True when local file appears binary by probing header bytes."""
        with path.open("rb") as handle:
            sample = handle.read(1024)
        return b"\x00" in sample

    def _is_binary_remote(self, remote_path: str) -> bool:
        """Return True when remote file appears binary by probing header bytes."""
        if self.connection.sftp is None:
            raise RuntimeError("SFTP session is not connected")
        with self.connection.sftp.open(remote_path, "rb") as handle:
            sample = handle.read(1024)
        return b"\x00" in sample

    def _apply_downloads(self, items: list[PurePosixPath], result: SyncResult) -> None:
        """Apply planned downloads with dry-run support."""
        for path in items:
            if self.dry_run:
                self.console.print(f"+ download {path.as_posix()}")
                result.downloaded.append(path)

        if self.dry_run or not items:
            return

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            future_to_path = {
                executor.submit(
                    download_file,
                    self.connection,
                    self.remote_dir,
                    self.local_dir,
                    path,
                    False,
                    self.verify,
                ): path
                for path in items
            }
            for future in concurrent.futures.as_completed(future_to_path):
                path = future_to_path[future]
                try:
                    future.result()
                    result.downloaded.append(path)
                except (OSError, RuntimeError) as exc:
                    LOGGER.error("Download failed for %s: %s", path.as_posix(), exc)
                    self._transfer_errors += 1
                    result.skipped.append(path)

    def _apply_uploads(self, items: list[PurePosixPath], result: SyncResult) -> None:
        """Apply planned uploads with dry-run support."""
        for path in items:
            if self.dry_run:
                self.console.print(f"+ upload {path.as_posix()}")
                result.uploaded.append(path)

        if self.dry_run or not items:
            return

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            future_to_path = {
                executor.submit(
                    upload_file,
                    self.connection,
                    self.local_dir,
                    self.remote_dir,
                    path,
                    False,
                    self.copy_links,
                    self.verify,
                ): path
                for path in items
            }
            for future in concurrent.futures.as_completed(future_to_path):
                path = future_to_path[future]
                try:
                    future.result()
                    result.uploaded.append(path)
                except (OSError, RuntimeError) as exc:
                    LOGGER.error("Upload failed for %s: %s", path.as_posix(), exc)
                    self._transfer_errors += 1
                    result.skipped.append(path)

    def _resolve_conflict(
        self,
        path: PurePosixPath,
        local_entries: dict[PurePosixPath, FileMetadata],
        remote_entries: dict[PurePosixPath, FileMetadata],
    ) -> str:
        """Resolve conflict according to configured policy or interactively."""
        if self.conflict_policy == "remote":
            return "remote"
        if self.conflict_policy == "local":
            return "local"
        if self.conflict_policy == "skip":
            return "skip"
        if self.conflict_policy == "newer":
            local_meta = local_entries.get(path)
            remote_meta = remote_entries.get(path)
            if local_meta is None:
                return "remote"
            if remote_meta is None:
                return "local"
            if math.isclose(local_meta.mtime, remote_meta.mtime, abs_tol=1e-3):
                return "skip"
            return "local" if local_meta.mtime > remote_meta.mtime else "remote"

        self.console.print(f"Conflict detected: {path.as_posix()}")
        self.console.print("1) Use remote version")
        self.console.print("2) Use local version")
        self.console.print("3) Keep both versions")
        self.console.print("4) Show diff")
        self.console.print("5) Skip")

        while True:
            choice = self.console.input("Select an option [1-5]: ").strip()
            if choice == "1":
                return "remote"
            if choice == "2":
                return "local"
            if choice == "3":
                return "both"
            if choice == "4":
                _ = (local_entries, remote_entries)
                self._show_diff(path)
                continue
            if choice == "5":
                return "skip"
            self.console.print("Invalid option. Choose 1-5.")

    def _show_diff(self, relative_path: PurePosixPath) -> None:
        """Display text diff for local and remote versions when available."""
        if self.connection.sftp is None:
            raise RuntimeError("SFTP session is not connected")

        local_path = self.local_dir / Path(relative_path.as_posix())
        remote_path = posixpath.join(self.remote_dir, relative_path.as_posix())

        try:
            if self._is_binary_local(local_path) or self._is_binary_remote(remote_path):
                self.console.print("Binary file detected, diff view not supported.")
                return
        except OSError:
            self.console.print("Unable to detect binary/text mode; continuing with text diff.")

        try:
            with local_path.open("r", encoding="utf-8", errors="replace") as lf:
                local_text = lf.readlines(200_000)
            local_text = [line.rstrip("\n") for line in local_text]
        except OSError:
            local_text = ["<unable to read local text>"]

        try:
            with self.connection.sftp.open(remote_path, "rb") as handle:
                remote_data = handle.read(200_000)
                remote_text = remote_data.decode("utf-8", errors="replace").splitlines()
        except OSError:
            remote_text = ["<unable to read remote text>"]

        diff = difflib.unified_diff(
            local_text,
            remote_text,
            fromfile=f"local/{relative_path.as_posix()}",
            tofile=f"remote/{relative_path.as_posix()}",
            lineterm="",
        )
        rendered = "\n".join(diff) or "No textual differences to display."
        self.console.print(rendered, markup=False, highlight=False)

    def _report_skips(self, items: list[PurePosixPath]) -> None:
        """Print skipped files in dry-run mode."""
        if not self.dry_run:
            return
        for path in items:
            self.console.print(f"- skip {path.as_posix()}")

    def _report_ignored(self, count: int) -> None:
        """Display skipped-by-ignore count in run output."""
        self.console.print(f"Ignored/skipped by scanner: {count}")

    def _keep_both(self, relative_path: PurePosixPath) -> None:
        """Keep both local and remote versions as `.local` and `.remote` files."""
        if self.connection.sftp is None:
            raise RuntimeError("SFTP session is not connected")

        remote_origin = posixpath.join(self.remote_dir, relative_path.as_posix())
        local_origin = self.local_dir / Path(relative_path.as_posix())

        local_copy = local_origin.with_name(f"{local_origin.stem}.local")
        remote_copy_local = local_origin.with_name(f"{local_origin.stem}.remote")

        if self.dry_run:
            self.console.print(f"+ keep-both {relative_path.as_posix()} -> {local_copy.name}, {remote_copy_local.name}")
            return

        ensure_parent(local_copy)
        ensure_parent(remote_copy_local)

        if local_origin.exists():
            shutil.copy2(local_origin, local_copy)

        with self.connection.sftp.open(remote_origin, "rb") as src:
            with remote_copy_local.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)

